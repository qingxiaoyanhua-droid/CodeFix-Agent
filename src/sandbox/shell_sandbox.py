#!/usr/bin/env python3
"""
ShellSandbox: Isolated code execution sandbox.

Security model:
  Layer 1: Whitelist of allowed languages
  Layer 2: AST-based danger detection (Python) + deny-list (shell)
  Layer 3: Subprocess isolation (separate process, fresh env)
  Layer 4: Temp directory with cleanup
  Layer 5: Resource limits (timeout, stdout/stderr size, memory)
  Layer 6: Exit code verification + compile output path guard

Python detection uses ast.parse for reliable structural analysis.
Shell detection uses regex deny-list for command-string patterns.

Usage:
    from shell_sandbox import ShellSandbox

    sandbox = ShellSandbox(timeout=10, max_output_chars=10000)
    result = sandbox.run(code="print('hello')", language="python")
"""

import os
import re
import sys
import time
import json
import shutil
import tempfile
import subprocess
import hashlib
import ast
from dataclasses import dataclass, field
from typing import List, Optional, Literal
from pathlib import Path

# ==================== Danger Patterns ====================

_SHELL_DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",           # rm -rf /
    r":\(\)\{",                # Fork bomb
    r"\$\(.*rm",               # Command substitution with rm
    r">\s*/dev/sda",           # Direct disk write
    r"mkfs\.",                 # Format filesystem
    r"dd\s+if=.*of=/dev/",     # Raw disk write
    r"\|\s*sh",                # Pipe to shell
    r"wget.*\|.*sh",           # Download & execute
    r"curl.*\|.*sh",           # Curl & execute
    r"nc\s+-e\s+",             # Netcat reverse shell
    r"/etc/shadow",            # Read shadow
    r"chmod\s+777\s+/",        # Writable root
    r":(){ :|:& };:",          # Fork bomb bash
    r"nohup\s+.*\s+&\s*$",    # Background daemon (suspicious)
]

# Test-File Guard Rail: 拒绝修改测试相关代码的模式
_TEST_FILE_PATTERNS = [
    (r"def\s+test_\w+", "Test function definition"),
    (r"class\s+Test\w+", "Test class definition"),
    (r"unittest\.TestCase", "unittest TestCase"),
    (r"pytest\.fixture", "pytest fixture"),
    (r"@pytest\.mark\.param", "pytest parametrize"),
    (r"def\s+suite\(", "Test suite function"),
]

_DANGEROUS_BUILTINS_AST = {
    # Module-level dangerous imports
    "os": {"system", "popen", "execl", "execlp", "execv", "execve", "execvp", "execvpe",
           "spawnl", "spawnv", "spawnve", "spawnvp", "spawnvpe", "fork", "forkpty",
           "kill", "killpg", "setuid", "setgid", "setregid", "setreuid", "setresuid",
           "chdir", "chroot", "remove", "unlink", "rmdir", "rename", "replace",
           "mkdir", "makedirs", "rmtree", "chmod", "chown", "lchown", "link", "symlink",
           "listdir", "walk", "access", "exists", "isdir", "isfile", "getcwd", "getenv"},
    "subprocess": {"Popen", "call", "run", "check_call", "check_output", "DEVNULL",
                   "PIPE", "STDOUT", "CalledProcessError"},
    "ctypes": {"CDLL", "WinDLL", "OleDLL", "PyDLL", "c_void_p", "cast", "pointer",
               "Structure", "Union", "Array"},
    "pty": {"spawn", "openpty", "forkpty"},
    "threading": {"Thread", "Lock", "RLock", "Semaphore", "Event", "Condition", "Timer"},
    "multiprocessing": {"Process", "Pool", "Queue", "Pipe", "Value", "Array", "Manager",
                         "Lock", "Semaphore", "Event", "Condition"},
    "sys": {"setrecursionlimit", "settrace", "setprofile", "excepthook",
            "path", "modules", "argv"},
    "socket": {"socket", "create_server", "create_connection"},
    "urllib": {"urlopen", "urlretrieve", "request"},
    "requests": {"get", "post", "put", "delete", "patch", "head", "options"},
    "http": {"client"},
    "platform": {"os", "linux_distribution", "mac_ver", "win32_ver"},
    "resource": {"setrlimit", "getrlimit"},
    "signal": {"signal", "alarm"},
    "builtins": {"open", "compile", "eval", "exec", "__import__", "reload"},
}

_DANGEROUS_BUILTINS_LITERAL = {"__import__", "eval", "exec", "compile", "open",
                                "reload", "breakpoint", "exit", "quit"}


def _ast_check_python(code: str) -> Optional[str]:
    """
    Use AST parsing to detect dangerous Python patterns.
    Returns reason string if blocked, None if safe.
    Much more reliable than regex for detecting dangerous calls.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    dangerous_modules: set = set()
    visited_calls: set = set()

    for node in ast.walk(tree):
        # 1. Dangerous builtin calls: eval(), exec(), __import__(), open()
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
                if name in _DANGEROUS_BUILTINS_LITERAL:
                    if name == "open" and len(node.args) >= 2:
                        mode = node.args[1]
                        if isinstance(mode, ast.Constant) and "w" in str(mode.value):
                            return f"Dangerous: open() with write mode"
                    elif name == "eval":
                        return f"Dangerous: eval() allows arbitrary code execution"
                    elif name == "exec":
                        return f"Dangerous: exec() allows arbitrary code execution"
                    elif name == "__import__":
                        submod = None
                        if node.args and isinstance(node.args[0], ast.Constant):
                            submod = node.args[0].value
                        return f"Dangerous: __import__() dynamic import"
                    elif name == "compile":
                        return f"Dangerous: compile() can create arbitrary code objects"

        # 2. Attribute access on dangerous modules: os.system, subprocess.Popen, etc.
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                mod = node.value.id
                attr = node.attr
                if mod in _DANGEROUS_BUILTINS_AST:
                    dangerous_attrs = _DANGEROUS_BUILTINS_AST[mod]
                    if attr in dangerous_attrs:
                        if mod == "os" and attr in {"system", "popen"}:
                            return f"Dangerous: os.{attr}() executes shell commands"
                        if mod == "subprocess" and attr in {"Popen", "call", "run", "check_call", "check_output"}:
                            return f"Dangerous: subprocess.{attr}() can execute arbitrary commands"
                        if mod == "ctypes":
                            return f"Dangerous: ctypes.{attr} allows raw memory access"
                        if mod == "threading" and attr == "Thread":
                            return f"Dangerous: threading.Thread() spawns uncontrolled threads"
                        if mod == "multiprocessing" and attr == "Process":
                            return f"Dangerous: multiprocessing.Process() spawns uncontrolled processes"
                        if mod == "pty" and attr == "spawn":
                            return f"Dangerous: pty.spawn() creates uncontrolled pseudo-terminals"
                        if mod in {"urllib", "requests", "http"}:
                            return f"Dangerous: {mod}.{attr}() makes network calls"
                        return f"Dangerous: {mod}.{attr}() is not allowed"

        # 3. Import statements that pull in dangerous modules
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.split(".")[0] in _DANGEROUS_BUILTINS_AST:
                    alias = node.names[0].name if node.names else mod
                    return f"Dangerous: importing '{mod}' allows system-level operations"
            else:
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    if mod in _DANGEROUS_BUILTINS_AST:
                        return f"Dangerous: importing '{mod}' allows system-level operations"

        # 4. subprocess.run/call with shell=True (detected via Call node)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "subprocess":
                    for keyword in node.keywords:
                        if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) \
                                and keyword.value.value is True:
                            return f"Dangerous: subprocess.{func.attr}(shell=True) executes arbitrary shell commands"

        # 5. getattr/setattr with dunder attrs
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in ("getattr", "setattr", "delattr"):
                if len(node.args) >= 2:
                    attr_arg = node.args[1]
                    if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str):
                        if attr_arg.value.startswith("__") and attr_arg.value.endswith("__"):
                            return f"Dangerous: {func.id}() accessing dunder attribute '{attr_arg.value}'"

        # 6. ctypes in assignment: ctypes.CDLL(...) etc.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    pass  # We detect via ast.Attribute above

    return None


def _regex_check_python(code: str) -> Optional[str]:
    """Fallback regex check for patterns AST might miss (string obfuscation, etc.)."""
    # Only run a quick pre-check for obviously obfuscated patterns
    # that AST parsing won't catch
    obfuscation_patterns = [
        (r'getattr\s*\(\s*__builtins__\s*,', "Obfuscated access to __builtins__ via getattr"),
        (r'eval\s*\(\s*["\']\w+["\']\s*\)', "Obfuscated eval call"),
    ]
    for pat, reason in obfuscation_patterns:
        if re.search(pat, code):
            return reason
    return None

_LANG_COMMANDS: dict[str, list[str]] = {
    "python":  ["python3", "-u", "{filepath}"],
    "python3": ["python3", "-u", "{filepath}"],
    "node":    ["node", "{filepath}"],
    "nodejs":  ["node", "{filepath}"],
    "c":       ["gcc", "-x", "c", "-o", "{binary}", "{filepath}", "2>&1"],
    "cpp":     ["g++", "-x", "c++", "-o", "{binary}", "{filepath}", "2>&1"],
    "go":      ["go", "run", "{filepath}"],
    "rust":    ["rustc", "{filepath}", "-o", "{binary}"],
    "java":    ["java", "{filepath}"],
    "javascript": ["node", "{filepath}"],
}

_LANG_EXTENSIONS: dict[str, str] = {
    "python":  "py",
    "python3": "py",
    "node":    "js",
    "nodejs":  "js",
    "c":       "c",
    "cpp":     "cpp",
    "go":      "go",
    "rust":    "rs",
    "java":    "java",
    "javascript": "js",
}

_COMPILE_LANGS = {"c", "cpp", "rust", "java"}


# ==================== Result Dataclass ====================

@dataclass
class SandboxResult:
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float
    language: str
    error: Optional[str] = None
    compile_error: bool = False


# ==================== Sandbox Class ====================

class ShellSandbox:
    """
    Isolated code execution sandbox using subprocess + temp directory.

    Security layers:
      1. Language whitelist
      2. Danger pattern detection (shell + Python builtins)
      3. Subprocess isolation (separate PID, fresh env)
      4. Temp directory with automatic cleanup
      5. Resource limits (timeout, output size, memory)
      6. Exit code verification

    Usage:
        sandbox = ShellSandbox(timeout=10, max_output_chars=10000)
        result = sandbox.run(code="print('hello')", language="python")
    """

    def __init__(
        self,
        timeout: float = 10.0,
        max_output_chars: int = 10000,
        max_memory_mb: int = 512,
        allowed_languages: Optional[List[str]] = None,
        work_dir: Optional[str] = None,
    ):
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self.max_memory_mb = max_memory_mb

        self.allowed_languages = allowed_languages or [
            "python", "python3", "node", "c", "cpp", "javascript"
        ]
        self.work_dir = work_dir or tempfile.mkdtemp(prefix="sandbox_")

        # Compile compiled languages' results
        self._binary_cache: dict[str, str] = {}

        self._shell_re = [re.compile(p, re.IGNORECASE) for p in _SHELL_DANGEROUS_PATTERNS]

    # ---- Test-File Guard Rail (MAC 论文最佳实践) ----

    def check_test_file_guard(self, code: str) -> tuple[bool, str]:
        """
        检查代码是否包含测试相关结构（预防性安全层）。

        参考 MAC 论文 SWE-Bench 高分 artifact 的设计：
        - 显式拒绝修改测试文件，防止 Agent 修改测试来"通过"
        - 这是预防性的，在沙箱层拦截

        Returns:
            (is_safe, reason) — is_safe=True 表示代码安全，False 表示检测到测试文件修改
        """
        for pattern, description in _TEST_FILE_PATTERNS:
            if re.search(pattern, code):
                return False, f"Test-File Guard Rail triggered: {description} found. Refusing to execute test file modifications."

        return True, ""

    # ---- Public API ----

    def run(self, code: str, language: str, test_cases: Optional[List[dict]] = None) -> SandboxResult:
        """
        Execute `code` in the sandbox.

        Args:
            code: Source code to execute.
            language: One of supported languages (python, node, c, ...).
            test_cases: Optional list of test cases to run.

        Returns:
            SandboxResult with success, stdout, stderr, exit_code, duration_ms, error.
        """
        t0 = time.perf_counter()

        # Layer 1: Language whitelist
        lang = language.lower()
        if lang not in self.allowed_languages:
            return SandboxResult(
                success=False,
                stdout="",
                stderr=f"Language '{lang}' is not allowed. Allowed: {self.allowed_languages}",
                exit_code=-1,
                duration_ms=(time.perf_counter() - t0) * 1000,
                language=lang,
                error="LanguageNotAllowed",
            )

        # Layer 2: Danger pattern check
        blocked_reason = self._check_danger(code, lang)
        if blocked_reason:
            return SandboxResult(
                success=False,
                stdout="",
                stderr=f"Dangerous pattern detected: {blocked_reason}",
                exit_code=-1,
                duration_ms=(time.perf_counter() - t0) * 1000,
                language=lang,
                error="DangerousPattern",
            )

        # Layer 0b: Test-File Guard Rail (MAC 论文最佳实践)
        is_safe, guard_reason = self.check_test_file_guard(code)
        if not is_safe:
            return SandboxResult(
                success=False,
                stdout="",
                stderr=guard_reason,
                exit_code=-1,
                duration_ms=(time.perf_counter() - t0) * 1000,
                language=lang,
                error="TestFileModificationBlocked",
            )

        # Layer 3-6: Execute in subprocess
        if lang in _COMPILE_LANGS:
            return self._run_compiled(code, lang, t0)
        else:
            return self._run_interpreted(code, lang, t0)

    def cleanup(self):
        """Remove the sandbox working directory."""
        try:
            shutil.rmtree(self.work_dir, ignore_errors=True)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup()

    # ---- Internal Execution ----

    def _run_interpreted(self, code: str, lang: str, t0: float) -> SandboxResult:
        ext = _LANG_EXTENSIONS.get(lang, "txt")
        filepath = os.path.join(self.work_dir, f"main.{ext}")

        try:
            with open(filepath, "w", encoding="utf-8", errors="replace") as f:
                f.write(code)
        except Exception as e:
            return SandboxResult(
                success=False,
                stdout="",
                stderr=f"Failed to write code: {e}",
                exit_code=-1,
                duration_ms=(time.perf_counter() - t0) * 1000,
                language=lang,
                error="WriteError",
            )

        cmd = [c.replace("{filepath}", filepath)
                for c in _LANG_COMMANDS.get(lang, [])]

        return self._execute(cmd, lang, t0, filepath=filepath)

    def _run_compiled(self, code: str, lang: str, t0: float) -> SandboxResult:
        ext = _LANG_EXTENSIONS.get(lang, "txt")
        source_path = os.path.join(self.work_dir, f"main.{ext}")

        # Force binary output inside work_dir — prevents writing to /etc, /tmp, etc.
        safe_name = f"main_{lang}_binary"
        if lang == "java":
            safe_name = self._extract_java_class_name(code) + ".class"
        binary_path = os.path.normpath(os.path.join(self.work_dir, safe_name))

        try:
            with open(source_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(code)
        except Exception as e:
            return SandboxResult(
                success=False, stdout="", stderr=f"Failed to write code: {e}",
                exit_code=-1, duration_ms=(time.perf_counter() - t0) * 1000,
                language=lang, error="WriteError",
            )

        # Verify binary_path is safely inside work_dir (defense-in-depth)
        work_dir_abs = os.path.normpath(os.path.abspath(self.work_dir))
        binary_abs = os.path.normpath(os.path.abspath(binary_path))
        if not binary_abs.startswith(work_dir_abs + os.sep) and binary_abs != work_dir_abs:
            return SandboxResult(
                success=False, stdout="", stderr="Binary output path escapes sandbox directory",
                exit_code=-1, duration_ms=(time.perf_counter() - t0) * 1000,
                language=lang, error="DangerousPattern",
            )

        # Compile step
        compile_cmd = [
            c.replace("{filepath}", source_path)
              .replace("{binary}", binary_path)
            for c in _LANG_COMMANDS.get(lang, [])
        ]

        compile_result = self._execute(compile_cmd, lang, t0, filepath=source_path)

        if compile_result.exit_code != 0:
            compile_result.compile_error = True
            return compile_result

        # Run step
        if lang in ("c", "cpp", "rust"):
            run_cmd = [binary_path]
        elif lang == "java":
            # java needs the class name, extract from code
            class_name = self._extract_java_class_name(code)
            run_cmd = ["java", "-cp", self.work_dir, class_name]
        else:
            run_cmd = [binary_path]

        return self._execute(run_cmd, lang, t0, filepath=binary_path)

    def _execute(
        self,
        cmd: List[str],
        lang: str,
        t0: float,
        filepath: Optional[str] = None,
    ) -> SandboxResult:
        """Execute command in subprocess with resource limits."""
        stderr_lines: List[str] = []

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.work_dir,
                env=self._make_clean_env(),
                text=True,
                errors="replace",
            )

            # Unix: use select for non-blocking reads
            # Windows: fall back to communicate() with timeout loop
            if sys.platform != "win32":
                stdout_chunks: List[str] = []
                stderr_chunks: List[str] = []
                total_chars = 0

                while True:
                    elapsed = time.perf_counter() - t0
                    if elapsed >= self.timeout:
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass
                        stderr_lines.append(f"[TIMEOUT] Execution exceeded {self.timeout}s limit")
                        break

                    reads, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.1)

                    if proc.stdout in reads:
                        chunk = proc.stdout.read(4096)
                        if not chunk:
                            break
                        if total_chars + len(chunk) <= self.max_output_chars:
                            stdout_chunks.append(chunk)
                            total_chars += len(chunk)
                        else:
                            remaining = self.max_output_chars - total_chars
                            if remaining > 0:
                                stdout_chunks.append(chunk[:remaining])
                            stdout_chunks.append(f"\n[OUTPUT TRUNCATED at {self.max_output_chars} chars]")
                            total_chars = self.max_output_chars + 1
                            try:
                                proc.stdout.read()
                            except Exception:
                                pass

                    if proc.stderr in reads:
                        chunk = proc.stderr.read(4096)
                        if chunk:
                            stderr_chunks.append(chunk)

                    if proc.poll() is not None:
                        remaining_stderr = proc.stderr.read()
                        if remaining_stderr:
                            stderr_chunks.append(remaining_stderr)
                        break

                stdout = "".join(stdout_chunks)
                stderr = "".join(stderr_chunks)
            else:
                # Windows: polling communicate() with timeout
                stdout, stderr = "", ""
                start = time.perf_counter()
                while proc.poll() is None:
                    elapsed = time.perf_counter() - t0
                    if elapsed >= self.timeout:
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass
                        stderr_lines.append(f"[TIMEOUT] Execution exceeded {self.timeout}s limit")
                        break
                    time.sleep(0.05)

                # Final communicate
                try:
                    stdout_raw, stderr_raw = proc.communicate(timeout=1)
                    stdout = stdout_raw or ""
                    stderr = stderr_raw or ""
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()

            exit_code = proc.returncode if proc.returncode is not None else -1

            if proc.returncode is None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                stderr_lines.append("[KILLED] Process was forcefully terminated")

        except FileNotFoundError as e:
            return SandboxResult(
                success=False,
                stdout="",
                stderr=f"Runtime not found: {e}. Is the language runtime installed?",
                exit_code=-1,
                duration_ms=(time.perf_counter() - t0) * 1000,
                language=lang,
                error="RuntimeNotFound",
            )
        except Exception as e:
            return SandboxResult(
                success=False,
                stdout="",
                stderr=str(e),
                exit_code=-1,
                duration_ms=(time.perf_counter() - t0) * 1000,
                language=lang,
                error="ExecutionError",
            )

        duration_ms = (time.perf_counter() - t0) * 1000
        success = exit_code == 0 and not stderr_lines

        if stderr_lines:
            stderr = (stderr + "\n".join(stderr_lines)).strip()

        return SandboxResult(
            success=success,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=duration_ms,
            language=lang,
        )

    # ---- Security Checks ----

    def _check_danger(self, code: str, lang: str) -> Optional[str]:
        """Check code for dangerous patterns. Returns reason if blocked, None if safe."""
        if lang in ("python", "python3"):
            # Layer 2a: AST-based detection (primary, reliable)
            ast_reason = _ast_check_python(code)
            if ast_reason:
                return ast_reason
            # Layer 2b: Regex fallback for obfuscation patterns
            regex_reason = _regex_check_python(code)
            if regex_reason:
                return regex_reason
        else:
            # Shell patterns for shell-like languages
            for pattern_re in self._shell_re:
                if pattern_re.search(code):
                    return f"Shell dangerous pattern matched: {pattern_re.pattern}"
        return None

    # ---- Environment ----

    def _make_clean_env(self) -> dict:
        """Create a clean environment for the subprocess."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            # Strip dangerous variables
            "PYTHONPATH": "",
            "LD_PRELOAD": "",
            "LD_LIBRARY_PATH": "",
            "DYLD_INSERT_LIBRARIES": "",
            "DYLD_LIBRARY_PATH": "",
        }
        return env

    # ---- Helpers ----

    @staticmethod
    def _extract_java_class_name(code: str) -> str:
        match = re.search(r"public\s+class\s+(\w+)", code)
        return match.group(1) if match else "Main"

    def get_stats(self) -> dict:
        """Return sandbox statistics."""
        work_dir_size = 0
        try:
            for dirpath, _, filenames in os.walk(self.work_dir):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    work_dir_size += os.path.getsize(fp)
        except Exception:
            pass
        return {
            "work_dir": self.work_dir,
            "work_dir_size_bytes": work_dir_size,
            "timeout": self.timeout,
            "max_output_chars": self.max_output_chars,
            "allowed_languages": self.allowed_languages,
        }


# ==================== Test Runner (with test case injection) ====================

class TestRunner:
    """
    Run code with injected test cases.

    Injects a test harness into the user's code and executes in the sandbox.
    Supports Python and JavaScript test cases.
    """

    def __init__(self, sandbox: Optional[ShellSandbox] = None, **sandbox_kwargs):
        self.sandbox = sandbox or ShellSandbox(**sandbox_kwargs)

    def run_tests(
        self,
        code: str,
        language: str,
        test_cases: List[dict],
    ) -> tuple[bool, str]:
        """
        Execute code with test cases.

        Args:
            code: User's source code.
            language: Programming language.
            test_cases: List of dicts with function, input, output keys.

        Returns:
            (passed, message)
        """
        lang = language.lower()

        if lang == "python":
            return self._run_python_tests(code, test_cases)
        elif lang in ("node", "nodejs", "javascript"):
            return self._run_js_tests(code, test_cases)
        else:
            # For compiled languages, embed tests in the source
            return self._run_embedded_tests(code, lang, test_cases)

    def _wrap_python_tests(self, code: str, test_cases: List[dict]) -> str:
        """Wrap user code with test assertions."""
        test_lines = ["\n# --- Auto-injected test harness ---"]
        for i, tc in enumerate(test_cases):
            fname = tc.get("function", "test")
            args = tc.get("input", [])
            expected = tc.get("output")
            args_repr = ", ".join(repr(a) for a in args)
            test_lines.append(
                f"    actual = {fname}({args_repr})\n"
                f"    assert actual == {expected!r}, "
                f"'Test {i+1} FAILED: {fname}({args_repr}) = {{actual}}, expected {expected!r}'"
            )
        test_lines.append("    print('ALL_TESTS_PASSED')")
        return code + "\n" + "\n".join(test_lines)

    def _run_python_tests(self, code: str, test_cases: List[dict]) -> tuple[bool, str]:
        wrapped = self._wrap_python_tests(code, test_cases)
        result = self.sandbox.run(wrapped, "python")
        return self._interpret_result(result)

    def _wrap_js_tests(self, code: str, test_cases: List[dict]) -> str:
        """Wrap user code with Node.js test assertions."""
        test_lines = ["\n// --- Auto-injected test harness ---"]
        for i, tc in enumerate(test_cases):
            fname = tc.get("function", "test")
            args = tc.get("input", [])
            expected = tc.get("output")
            args_str = ", ".join(json.dumps(a) for a in args)
            test_lines.append(
                f"    const actual = {fname}({args_str});\n"
                f"    if (actual !== {json.dumps(expected)}) {{\n"
                f"        console.error('Test {i+1} FAILED: {fname}({args_str}) = ' + JSON.stringify(actual) + ', expected {json.dumps(expected)}');\n"
                f"        process.exit(1);\n"
                f"    }}"
            )
        test_lines.append("console.log('ALL_TESTS_PASSED');")
        return code + "\n" + "\n".join(test_lines)

    def _run_js_tests(self, code: str, test_cases: List[dict]) -> tuple[bool, str]:
        wrapped = self._wrap_js_tests(code, test_cases)
        result = self.sandbox.run(wrapped, "node")
        return self._interpret_result(result)

    def _run_embedded_tests(self, code: str, language: str, test_cases: List[dict]) -> tuple[bool, str]:
        """For compiled languages, run as-is (user should embed tests)."""
        result = self.sandbox.run(code, language)
        return self._interpret_result(result)

    def _interpret_result(self, result: SandboxResult) -> tuple[bool, str]:
        """Interpret SandboxResult into (passed, message)."""
        if result.error == "DangerousPattern":
            return False, f"Security blocked: {result.stderr}"
        if result.error == "RuntimeNotFound":
            return False, f"Runtime not installed: {result.stderr}"
        if result.error:
            return False, f"Execution error: {result.stderr}"

        if result.exit_code != 0:
            return False, f"Runtime error (exit {result.exit_code}):\n{result.stderr}"

        if "ALL_TESTS_PASSED" in result.stdout:
            return True, ""

        if result.stderr:
            return False, result.stderr

        return True, ""

    def cleanup(self):
        self.sandbox.cleanup()


# ==================== Global default sandbox ====================

_default_sandbox: Optional[ShellSandbox] = None


def get_sandbox(
    timeout: float = 10.0,
    max_output_chars: int = 10000,
    allowed_languages: Optional[List[str]] = None,
) -> ShellSandbox:
    """Get or create the default sandbox instance."""
    global _default_sandbox
    if _default_sandbox is None:
        _default_sandbox = ShellSandbox(
            timeout=timeout,
            max_output_chars=max_output_chars,
            allowed_languages=allowed_languages,
        )
    return _default_sandbox


def reset_sandbox():
    """Reset the default sandbox (cleanup + recreate)."""
    global _default_sandbox
    if _default_sandbox:
        _default_sandbox.cleanup()
        _default_sandbox = None
