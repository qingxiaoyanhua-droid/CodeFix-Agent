"""
Advanced bug injection script for the RepoBugFix Agent evaluation.
Contains 25+ different small, controllable bugs for comprehensive testing.
"""

import os
from pathlib import Path
from typing import Dict, Callable
import re


def inject_iter_slices_pos_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/utils.py -> iter_slices: set pos=0 to pos=7
    """
    target = Path(repo_path) / "src/requests/utils.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find iter_slices function and the line 'pos = 0'
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def iter_slices("):
            in_func = True
        elif in_func and "pos = 0" in line and line.strip().startswith(" "):
            # Replace pos = 0 with pos = 7
            lines[i] = line.replace("pos = 0", "pos = 7", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def iter_slices("):
            break
        elif in_func and line.strip() == "":
            # End of function
            break

    if not changed:
        raise RuntimeError("Failed to inject iter_slices bug (pos=0 not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: iter_slices pos=0 -> pos=7 in src/requests/utils.py"


def inject_url_normalize_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/models.py -> prepare_url: remove normalization
    """
    target = Path(repo_path) / "src/requests/models.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find prepare_url function and modify URL normalization
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def prepare_url("):
            in_func = True
        elif in_func and "normalize=url_norm" in line:
            # Remove normalization
            lines[i] = line.replace("normalize=url_norm", "normalize=False", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def prepare_url("):
            break

    if not changed:
        raise RuntimeError("Failed to inject url normalize bug (normalize=url_norm not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: url normalize disabled in prepare_url"


def inject_header_validation_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/utils.py -> check_header_validity: disable validation
    """
    target = Path(repo_path) / "src/requests/utils.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find check_header_validity function and disable validation
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def check_header_validity("):
            in_func = True
        elif in_func and "raise InvalidHeader" in line:
            # Comment out the validation
            lines[i] = "    # " + line.lstrip()  # Comment out the raise statement
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def check_header_validity("):
            break

    if not changed:
        raise RuntimeError("Failed to inject header validation bug (raise InvalidHeader not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: header validation disabled in check_header_validity"


def inject_timeout_conversion_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/adapters.py -> send: incorrect timeout conversion
    """
    target = Path(repo_path) / "src/requests/adapters.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find send function and modify timeout handling
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def send("):
            in_func = True
        elif in_func and "timeout =" in line and "urllib3.Timeout" in line:
            # Change timeout conversion
            lines[i] = line.replace("timeout = urllib3.Timeout.from_float(timeout)", 
                                   "timeout = urllib3.Timeout(total=timeout*1000)", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def send("):
            break

    if not changed:
        raise RuntimeError("Failed to inject timeout conversion bug (timeout conversion not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: timeout conversion changed in adapters send method"


def inject_cookie_domain_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/cookies.py -> merge_cookies: incorrect domain handling
    """
    target = Path(repo_path) / "src/requests/cookies.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find merge_cookies function and modify domain handling
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def merge_cookies("):
            in_func = True
        elif in_func and "for name, value in cookie_dict.items()" in line:
            # Introduce incorrect domain handling
            next_line = i + 1
            if next_line < len(lines) and lines[next_line].strip().startswith("for"):
                # Insert incorrect domain handling
                spaces = len(lines[next_line]) - len(lines[next_line].lstrip())
                indent = " " * spaces
                lines.insert(next_line, f"{indent}if name == 'sessionid':\n")
                lines.insert(next_line + 1, f"{indent}    continue  # Skip session cookies\n")
                changed = True
                break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def merge_cookies("):
            break

    if not changed:
        raise RuntimeError("Failed to inject cookie domain bug (merge_cookies structure not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: cookie domain handling changed in merge_cookies"


def inject_content_type_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/utils.py -> get_encoding_from_headers: incorrect charset extraction
    """
    target = Path(repo_path) / "src/requests/utils.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find get_encoding_from_headers function and modify charset extraction
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def get_encoding_from_headers("):
            in_func = True
        elif in_func and "charset" in line and "=" in line and "params" in line:
            # Modify charset extraction to be incorrect
            lines[i] = line.replace("charset", "charset.upper()", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def get_encoding_from_headers("):
            break

    if not changed:
        raise RuntimeError("Failed to inject content type bug (charset extraction not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: content type charset extraction changed in get_encoding_from_headers"


def inject_proxies_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/utils.py -> should_bypass_proxies: incorrect proxy bypass logic
    """
    target = Path(repo_path) / "src/requests/utils.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find should_bypass_proxies function and modify bypass logic
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def should_bypass_proxies("):
            in_func = True
        elif in_func and "return True" in line and "no_proxy" in line:
            # Change bypass logic to always return False
            lines[i] = line.replace("return True", "return False", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def should_bypass_proxies("):
            break

    if not changed:
        raise RuntimeError("Failed to inject proxies bug (should_bypass_proxies logic not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: proxy bypass logic changed in should_bypass_proxies"


def inject_auth_encoding_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/auth.py -> _basic_auth_str: incorrect encoding
    """
    target = Path(repo_path) / "src/requests/auth.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find _basic_auth_str function and modify encoding
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def _basic_auth_str("):
            in_func = True
        elif in_func and "username.encode" in line and "password.encode" in line:
            # Change encoding to be incorrect
            lines[i] = line.replace("utf-8", "ascii", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def _basic_auth_str("):
            break

    if not changed:
        raise RuntimeError("Failed to inject auth encoding bug (_basic_auth_str encoding not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: auth encoding changed in _basic_auth_str"


def inject_session_merge_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/sessions.py -> merge_environment_settings: incorrect merge
    """
    target = Path(repo_path) / "src/requests/sessions.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find merge_environment_settings function and modify merge logic
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def merge_environment_settings("):
            in_func = True
        elif in_func and "env_proxies" in line and "proxies" in line and "or" in line:
            # Change merge logic to be incorrect
            lines[i] = line.replace("proxies or env_proxies", "env_proxies or proxies", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def merge_environment_settings("):
            break

    if not changed:
        raise RuntimeError("Failed to inject session merge bug (merge_environment_settings logic not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: session merge logic changed in merge_environment_settings"


def inject_redirect_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/sessions.py -> resolve_redirects: incorrect redirect handling
    """
    target = Path(repo_path) / "src/requests/sessions.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find resolve_redirects function and modify redirect logic
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def resolve_redirects("):
            in_func = True
        elif in_func and "while" in line and "resp" in line and "history" in line:
            # Add incorrect redirect handling
            next_line = i + 1
            if next_line < len(lines) and lines[next_line].strip().startswith("yield"):
                spaces = len(lines[next_line]) - len(lines[next_line].lstrip())
                indent = " " * spaces
                lines.insert(next_line, f"{indent}if len(history) > 1:\n")
                lines.insert(next_line + 1, f"{indent}    break  # Stop after first redirect\n")
                changed = True
                break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def resolve_redirects("):
            break

    if not changed:
        raise RuntimeError("Failed to inject redirect bug (resolve_redirects structure not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: redirect handling changed in resolve_redirects"


def inject_json_encoding_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/models.py -> json: incorrect encoding detection
    """
    target = Path(repo_path) / "src/requests/models.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find json property and modify encoding detection
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if "def json" in line and "@" in line:  # Property decorator
            in_func = True
        elif in_func and "json.loads" in line:
            # Change encoding to be incorrect
            lines[i] = line.replace("utf-8", "latin-1", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and "json" not in line:
            break

    if not changed:
        raise RuntimeError("Failed to inject json encoding bug (json property not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: json encoding changed in json property"


def inject_file_upload_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/models.py -> prepare_content_length: incorrect length calculation
    """
    target = Path(repo_path) / "src/requests/models.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find prepare_content_length function and modify length calculation
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def prepare_content_length("):
            in_func = True
        elif in_func and "len(body)" in line and "content_length" in line:
            # Change length calculation to be incorrect
            lines[i] = line.replace("len(body)", "len(body) // 2", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def prepare_content_length("):
            break

    if not changed:
        raise RuntimeError("Failed to inject file upload bug (prepare_content_length not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: file upload length calculation changed in prepare_content_length"


def inject_ssl_verification_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/adapters.py -> send: incorrect SSL verification
    """
    target = Path(repo_path) / "src/requests/adapters.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find send function and modify SSL verification
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def send("):
            in_func = True
        elif in_func and "verify =" in line and "kw" in line:
            # Change SSL verification to be incorrect
            lines[i] = line.replace("verify", "not verify", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def send("):
            break

    if not changed:
        raise RuntimeError("Failed to inject ssl verification bug (SSL verification not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: SSL verification changed in adapters send method"


def inject_chunked_encoding_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/models.py -> prepare_body: incorrect chunked encoding
    """
    target = Path(repo_path) / "src/requests/models.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find prepare_body function and modify chunked encoding
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def prepare_body("):
            in_func = True
        elif in_func and "Transfer-Encoding" in line and "chunked" in line:
            # Change chunked encoding logic
            lines[i] = line.replace("chunked", "gzip", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def prepare_body("):
            break

    if not changed:
        raise RuntimeError("Failed to inject chunked encoding bug (Transfer-Encoding not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: chunked encoding changed in prepare_body"


def inject_cache_control_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/compat.py -> is_fp_closed: incorrect file pointer check
    """
    target = Path(repo_path) / "src/requests/compat.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find is_fp_closed function and modify file pointer check
    lines = content.splitlines(keepends=True)
    
    for i, line in enumerate(lines):
        if "def is_fp_closed(" in line:
            # Find the return statement
            for j in range(i, min(i+10, len(lines))):
                if "return" in lines[j] and "closed" in lines[j]:
                    # Change the logic to be incorrect
                    lines[j] = lines[j].replace("closed", "not closed", 1)
                    target.write_text("".join(lines), encoding="utf-8")
                    return "Injected bug: cache control changed in is_fp_closed"
    
    raise RuntimeError("Failed to inject cache control bug (is_fp_closed not found).")


def inject_connection_pool_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/adapters.py -> close: incorrect connection pool handling
    """
    target = Path(repo_path) / "src/requests/adapters.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find close function and modify connection pool handling
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def close("):
            in_func = True
        elif in_func and "self.poolmanager" in line and "clear" in line:
            # Comment out the clear operation
            lines[i] = "    # " + line.lstrip()  # Comment out the clear operation
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def close("):
            break

    if not changed:
        raise RuntimeError("Failed to inject connection pool bug (close function not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: connection pool handling changed in close method"


def inject_retry_logic_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/sessions.py -> rebuild_method: incorrect retry logic
    """
    target = Path(repo_path) / "src/requests/sessions.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find rebuild_method function and modify retry logic
    lines = content.splitlines(keepends=True)
    
    for i, line in enumerate(lines):
        if "def rebuild_method(" in line:
            # Find where method is set
            for j in range(i, min(i+20, len(lines))):
                if "method =" in lines[j] and "in" in lines[j]:
                    # Change the logic to be incorrect
                    lines[j] = lines[j].replace("method in", "method not in", 1)
                    target.write_text("".join(lines), encoding="utf-8")
                    return "Injected bug: retry logic changed in rebuild_method"
    
    raise RuntimeError("Failed to inject retry logic bug (rebuild_method not found).")


def inject_content_decoding_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/models.py -> content: incorrect decoding
    """
    target = Path(repo_path) / "src/requests/models.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find content property and modify decoding
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if "def content" in line and "@" in line:  # Property decorator
            in_func = True
        elif in_func and "decode" in line and "encoding" in line:
            # Change decoding to be incorrect
            lines[i] = line.replace("encoding", "'ascii'", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and "content" not in line:
            break

    if not changed:
        raise RuntimeError("Failed to inject content decoding bug (content property not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: content decoding changed in content property"


def inject_status_code_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/models.py -> raise_for_status: incorrect status code handling
    """
    target = Path(repo_path) / "src/requests/models.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find raise_for_status function and modify status code handling
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def raise_for_status("):
            in_func = True
        elif in_func and "400 >=" in line or "400 <=" in line:
            # Change the condition to be incorrect
            lines[i] = line.replace(">=", "<=", 1).replace("<=", ">=", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def raise_for_status("):
            break

    if not changed:
        raise RuntimeError("Failed to inject status code bug (raise_for_status condition not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: status code handling changed in raise_for_status"


def inject_request_method_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/api.py -> request: incorrect method handling
    """
    target = Path(repo_path) / "src/requests/api.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find request function and modify method handling
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def request("):
            in_func = True
        elif in_func and "method =" in line and "upper" in line:
            # Change method to be incorrect
            lines[i] = line.replace("method.upper()", "method.lower()", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def request("):
            break

    if not changed:
        raise RuntimeError("Failed to inject request method bug (request method handling not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: request method handling changed in request function"


def inject_response_history_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/models.py -> history: incorrect history handling
    """
    target = Path(repo_path) / "src/requests/models.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find history property and modify history handling
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if "def history" in line and "@" in line:  # Property decorator
            in_func = True
        elif in_func and "list(" in line and "responses" in line:
            # Change history to be incorrect
            lines[i] = line.replace("list(", "list(reversed(", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and "history" not in line:
            break

    if not changed:
        raise RuntimeError("Failed to inject response history bug (history property not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: response history handling changed in history property"


def inject_url_joining_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/models.py -> prepare_url: incorrect URL joining
    """
    target = Path(repo_path) / "src/requests/models.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find prepare_url function and modify URL joining
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def prepare_url("):
            in_func = True
        elif in_func and "urljoin" in line and "base_url" in line:
            # Change URL joining to be incorrect
            lines[i] = line.replace("urljoin", "base_url +", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def prepare_url("):
            break

    if not changed:
        raise RuntimeError("Failed to inject url joining bug (prepare_url not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: URL joining changed in prepare_url"


def inject_header_case_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/structures.py -> CaseInsensitiveDict: incorrect case handling
    """
    target = Path(repo_path) / "src/requests/structures.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find CaseInsensitiveDict and modify case handling
    lines = content.splitlines(keepends=True)
    
    for i, line in enumerate(lines):
        if "class CaseInsensitiveDict" in line:
            # Find the __setitem__ method
            for j in range(i, min(i+50, len(lines))):
                if "__setitem__" in lines[j] and "def" in lines[j]:
                    # Find where key is converted to lower
                    for k in range(j, min(j+10, len(lines))):
                        if "key.lower()" in lines[k]:
                            # Change to upper instead of lower
                            lines[k] = lines[k].replace(".lower()", ".upper()", 1)
                            target.write_text("".join(lines), encoding="utf-8")
                            return "Injected bug: header case handling changed in CaseInsensitiveDict"
    
    raise RuntimeError("Failed to inject header case bug (CaseInsensitiveDict not found).")


def inject_streaming_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/models.py -> iter_content: incorrect streaming
    """
    target = Path(repo_path) / "src/requests/models.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find iter_content method and modify streaming
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if "def iter_content(" in line and "def" in line:
            in_func = True
        elif in_func and "decode_content" in line and "stream" in line:
            # Change streaming to be incorrect
            lines[i] = line.replace("decode_content", "not decode_content", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and "iter_content" not in line:
            break

    if not changed:
        raise RuntimeError("Failed to inject streaming bug (iter_content not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: streaming handling changed in iter_content"


def inject_encoding_detection_bug(repo_path: str) -> str:
    """
    Inject bug: in src/requests/utils.py -> get_encodings_from_content: incorrect encoding detection
    """
    target = Path(repo_path) / "src/requests/utils.py"
    if not target.exists():
        raise FileNotFoundError(f"cannot find {target}")

    content = target.read_text(encoding="utf-8", errors="replace")
    
    # Find get_encodings_from_content function and modify detection
    lines = content.splitlines(keepends=True)
    
    in_func = False
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("def get_encodings_from_content("):
            in_func = True
        elif in_func and "charset" in line and "re.findall" in line:
            # Change encoding detection to be incorrect
            lines[i] = line.replace("charset", "encoding", 1)
            changed = True
            break
        # End function heuristic
        elif in_func and line.strip().startswith("def ") and not line.strip().startswith("def get_encodings_from_content("):
            break

    if not changed:
        raise RuntimeError("Failed to inject encoding detection bug (get_encodings_from_content not found).")
    
    target.write_text("".join(lines), encoding="utf-8")
    return "Injected bug: encoding detection changed in get_encodings_from_content"


# Collection of available bugs
BUG_INJECTORS: Dict[str, Callable[[str], str]] = {
    "iter_slices_pos": inject_iter_slices_pos_bug,
    "url_normalize": inject_url_normalize_bug,
    "header_validation": inject_header_validation_bug,
    "timeout_conversion": inject_timeout_conversion_bug,
    "cookie_domain": inject_cookie_domain_bug,
    "content_type": inject_content_type_bug,
    "proxies": inject_proxies_bug,
    "auth_encoding": inject_auth_encoding_bug,
    "session_merge": inject_session_merge_bug,
    "redirect": inject_redirect_bug,
    "json_encoding": inject_json_encoding_bug,
    "file_upload": inject_file_upload_bug,
    "ssl_verification": inject_ssl_verification_bug,
    "chunked_encoding": inject_chunked_encoding_bug,
    "cache_control": inject_cache_control_bug,
    "connection_pool": inject_connection_pool_bug,
    "retry_logic": inject_retry_logic_bug,
    "content_decoding": inject_content_decoding_bug,
    "status_code": inject_status_code_bug,
    "request_method": inject_request_method_bug,
    "response_history": inject_response_history_bug,
    "url_joining": inject_url_joining_bug,
    "header_case": inject_header_case_bug,
    "streaming": inject_streaming_bug,
    "encoding_detection": inject_encoding_detection_bug,
}


def list_available_bugs():
    """List all available bug injectors."""
    return list(BUG_INJECTORS.keys())


def inject_bug(repo_path: str, bug_type: str) -> str:
    """Inject a specific bug into the repository."""
    if bug_type not in BUG_INJECTORS:
        raise ValueError(f"Unknown bug type: {bug_type}. Available: {list_available_bugs()}")
    
    return BUG_INJECTORS[bug_type](repo_path)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <repo_path> <bug_type>")
        print(f"Available bug types: {list_available_bugs()}")
        sys.exit(1)
    
    repo_path = sys.argv[1]
    bug_type = sys.argv[2]
    
    try:
        result = inject_bug(repo_path, bug_type)
        print(result)
    except Exception as e:
        print(f"Error injecting bug: {e}")
        sys.exit(1)