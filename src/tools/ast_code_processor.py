#!/usr/bin/env python3
"""
基于Tree-sitter的AST代码处理引擎
核心功能：函数定位、函数替换、导入修复、依赖分析
完全解决行号偏移问题，支持Python/Java/JavaScript

参考聊天记录：豆包5.15聊天规划.md - 第一阶段：集成AST到现有修复流程
"""

import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from typing_extensions import TYPE_CHECKING
from dataclasses import dataclass

# 使用TYPE_CHECKING避免运行时依赖
if TYPE_CHECKING:
    from tree_sitter import Node

try:
    from tree_sitter import Language, Parser
    from tree_sitter_languages import get_language, get_parser
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    import ast as py_ast
    # 避免Windows控制台编码问题
    try:
        print("Warning: tree-sitter not installed, using Python standard ast module as fallback")
    except:
        pass


@dataclass
class FunctionInfo:
    """函数信息数据结构"""
    name: str
    start_byte: int
    end_byte: int
    start_point: Tuple[int, int]  # (行, 列)，Tree-sitter风格从0开始
    end_point: Tuple[int, int]
    node: Optional[object] = None


class ASTCodeProcessor:
    """
    基于Tree-sitter的代码AST处理引擎
    核心功能：函数定位、函数替换、导入修复、依赖分析
    完全解决行号偏移问题，支持Python/Java/JavaScript
    """
    
    SUPPORTED_LANGUAGES = {
        "python": {"ext": ".py", "function_node": "function_definition"},
        "java": {"ext": ".java", "function_node": "method_declaration"},
        "javascript": {"ext": ".js", "function_node": "function_declaration"}
    }
    
    def __init__(self, language: str = "python"):
        if language not in self.SUPPORTED_LANGUAGES:
            raise ValueError(f"不支持的语言: {language}，支持: {list(self.SUPPORTED_LANGUAGES.keys())}")
        
        self.language = language
        self.parser = None
        self.language_obj = None
        self.config = self.SUPPORTED_LANGUAGES[language]
        
        if TREE_SITTER_AVAILABLE:
            self._init_tree_sitter()
        else:
            self._init_fallback()
        
        # 预编译查询语句（性能优化）
        if TREE_SITTER_AVAILABLE:
            self._init_queries()
    
    def _init_tree_sitter(self):
        """初始化Tree-sitter解析器"""
        try:
            self.parser = get_parser(self.language)
            self.language_obj = get_language(self.language)
            print(f"✓ 加载Tree-sitter {self.language} 解析器")
        except Exception as e:
            print(f"⚠️ Tree-sitter初始化失败: {e}，使用降级方案")
            self._init_fallback()
    
    def _init_fallback(self):
        """降级到Python标准库"""
        self.parser = None
        self.language_obj = None
    
    def _init_queries(self):
        """初始化Tree-sitter查询"""
        if not self.language_obj:
            return
        
        self.function_query = self.language_obj.query(f"""
            ({self.config['function_node']}) @function
        """)
        
        self.import_query = self.language_obj.query("""
            (import_statement) @import
            (import_from_statement) @import_from
        """)
        
        self.class_query = self.language_obj.query("""
            (class_definition) @class
        """)
    
    def parse_code(self, code: str) -> Optional[object]:
        """解析代码生成AST根节点"""
        if not TREE_SITTER_AVAILABLE or not self.parser:
            return self._fallback_parse(code)
        
        try:
            return self.parser.parse(code.encode("utf-8")).root_node
        except Exception as e:
            print(f"⚠️ Tree-sitter解析失败: {e}")
            return self._fallback_parse(code)
    
    def _fallback_parse(self, code: str) -> Optional[object]:
        """降级方案：使用Python标准ast模块"""
        if self.language != "python":
            return None
        try:
            return py_ast.parse(code)
        except SyntaxError:
            return None
    
    def get_all_functions(self, code: str) -> List[FunctionInfo]:
        """
        获取文件中所有函数的信息
        返回: [FunctionInfo]
        """
        if TREE_SITTER_AVAILABLE and self.parser:
            return self._get_all_functions_tree_sitter(code)
        else:
            return self._get_all_functions_fallback(code)
    
    def _get_all_functions_tree_sitter(self, code: str) -> List[FunctionInfo]:
        """使用Tree-sitter获取函数列表"""
        tree = self.parse_code(code)
        if not tree:
            return []
        
        functions = []
        captures = self.function_query.captures(tree)
        
        for node, _ in captures:
            # 提取函数名
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
                
            function_name = name_node.text.decode("utf-8")
            functions.append(FunctionInfo(
                name=function_name,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                start_point=node.start_point,
                end_point=node.end_point,
                node=node
            ))
        
        return functions
    
    def _get_all_functions_fallback(self, code: str) -> List[FunctionInfo]:
        """降级方案：使用Python标准ast模块"""
        if self.language != "python":
            return []
        
        tree = self.parse_code(code)
        if not tree:
            return []
        
        functions = []
        code_bytes = code.encode("utf-8")
        
        for node in py_ast.walk(tree):
            if isinstance(node, py_ast.FunctionDef):
                # 计算字节偏移（近似）
                lines = code.split('\n')
                line_count = 0
                byte_count = 0
                start_byte = 0
                
                for i, line in enumerate(lines):
                    if i == node.lineno - 1:
                        start_byte = byte_count + node.col_offset
                        break
                    byte_count += len(line.encode('utf-8')) + 1  # +1 for newline
                
                # 估算结束位置
                end_byte = len(code_bytes)
                
                functions.append(FunctionInfo(
                    name=node.name,
                    start_byte=start_byte,
                    end_byte=end_byte,
                    start_point=(node.lineno - 1, node.col_offset),
                    end_point=(0, 0),
                    node=node
                ))
        
        return functions
    
    def find_function_by_name(self, code: str, function_name: str) -> Optional[FunctionInfo]:
        """精确查找指定名称的函数"""
        functions = self.get_all_functions(code)
        for func in functions:
            if func.name == function_name:
                return func
        return None
    
    def find_function_by_line_number(self, code: str, line_number: int) -> Optional[FunctionInfo]:
        """
        根据行号查找包含该行的函数
        解决编译器错误只给出行号的问题
        """
        functions = self.get_all_functions(code)
        for func in functions:
            # Tree-sitter行号从0开始，用户传入的行号从1开始
            start_line = func.start_point[0] + 1
            end_line = func.end_point[0] + 1
            if start_line <= line_number <= end_line:
                return func
        return None
    
    def replace_function(self, original_code: str, function_name: str, new_function_code: str) -> str:
        """
        精确替换指定函数，完全解决行号偏移问题
        核心方法：按字节偏移替换，而不是按行号替换
        """
        func_info = self.find_function_by_name(original_code, function_name)
        if not func_info:
            raise ValueError(f"函数 {function_name} 未找到")
        
        # 按字节偏移替换（最精确的方式，不受行号、空格、注释影响）
        code_bytes = original_code.encode("utf-8")
        new_code_bytes = (
            code_bytes[:func_info.start_byte] +
            new_function_code.encode("utf-8") +
            code_bytes[func_info.end_byte:]
        )
        
        return new_code_bytes.decode("utf-8")
    
    def replace_function_safe(self, original_code: str, function_name: str, new_function_code: str) -> Optional[str]:
        """安全版本的函数替换，失败返回None而不是抛出异常"""
        try:
            return self.replace_function(original_code, function_name, new_function_code)
        except Exception as e:
            print(f"⚠️ 函数替换失败: {e}")
            return None
    
    def fix_imports(self, code: str, missing_imports: List[str]) -> str:
        """
        自动修复缺失的导入语句
        将新导入添加到文件顶部，避免重复导入
        """
        if not missing_imports:
            return code
        
        if TREE_SITTER_AVAILABLE and self.parser:
            return self._fix_imports_tree_sitter(code, missing_imports)
        else:
            return self._fix_imports_fallback(code, missing_imports)
    
    def _fix_imports_tree_sitter(self, code: str, missing_imports: List[str]) -> str:
        """使用Tree-sitter修复导入"""
        tree = self.parse_code(code)
        if not tree:
            return self._fix_imports_fallback(code, missing_imports)
        
        imports = self.import_query.captures(tree)
        
        if not imports:
            # 没有导入语句，直接添加到文件开头
            return "\n".join(missing_imports) + "\n\n" + code
        
        # 找到最后一个导入语句的位置
        last_import_end = max(node.end_byte for node, _ in imports)
        
        # 检查是否已经存在这些导入
        existing_imports = set()
        for node, _ in imports:
            existing_imports.add(node.text.decode("utf-8").strip())
        
        new_imports = [imp for imp in missing_imports if imp.strip() not in existing_imports]
        
        if not new_imports:
            return code
        
        # 在最后一个导入语句之后添加新导入
        code_bytes = code.encode("utf-8")
        new_code_bytes = (
            code_bytes[:last_import_end] +
            b"\n" + "\n".join(new_imports).encode("utf-8") +
            code_bytes[last_import_end:]
        )
        
        return new_code_bytes.decode("utf-8")
    
    def _fix_imports_fallback(self, code: str, missing_imports: List[str]) -> str:
        """降级方案：简单的导入处理"""
        lines = code.split('\n')
        import_lines = []
        non_import_lines = []
        in_import_section = True
        
        for line in lines:
            stripped = line.strip()
            if in_import_section and (stripped.startswith('import ') or stripped.startswith('from ')):
                import_lines.append(line)
            else:
                in_import_section = False
                non_import_lines.append(line)
        
        existing_imports = set(import_lines)
        new_imports = [imp for imp in missing_imports if imp.strip() not in existing_imports]
        
        if not new_imports:
            return code
        
        # 在导入部分末尾添加新导入
        all_imports = import_lines + new_imports
        result = '\n'.join(all_imports) + '\n' + '\n'.join(non_import_lines)
        
        return result
    
    def get_function_dependencies(self, code: str, function_name: str) -> Dict[str, List[str]]:
        """
        分析函数的依赖关系
        返回: {"calls": ["被调用的函数名"], "imports": ["使用的导入"], "variables": ["使用的全局变量"]}
        """
        func_info = self.find_function_by_name(code, function_name)
        if not func_info:
            return {"calls": [], "imports": [], "variables": []}
        
        if TREE_SITTER_AVAILABLE and func_info.node:
            return self._get_dependencies_tree_sitter(code, func_info.node)
        else:
            return self._get_dependencies_fallback(code, function_name)
    
    def _get_dependencies_tree_sitter(self, code: str, func_node: object) -> Dict[str, List[str]]:
        """使用Tree-sitter分析依赖"""
        dependencies = {"calls": [], "imports": [], "variables": []}
        
        # 递归遍历函数体，查找函数调用
        def traverse(node: object):
            if node.type == "call":
                func_name_node = node.child_by_field_name("function")
                if func_name_node and func_name_node.type == "identifier":
                    dependencies["calls"].append(func_name_node.text.decode("utf-8"))
            
            for child in node.children:
                traverse(child)
        
        traverse(func_node)
        
        # 去重
        dependencies["calls"] = list(set(dependencies["calls"]))
        return dependencies
    
    def _get_dependencies_fallback(self, code: str, function_name: str) -> Dict[str, List[str]]:
        """降级方案：简单的依赖分析"""
        dependencies = {"calls": [], "imports": [], "variables": []}
        
        # 简单的正则匹配函数调用
        func_pattern = re.compile(r'\b([a-zA-Z_]\w*)\s*\(')
        matches = func_pattern.findall(code)
        
        # 过滤掉内置函数和关键字
        builtins = {'print', 'len', 'range', 'list', 'dict', 'set', 'str', 'int', 'float', 'bool'}
        dependencies["calls"] = list(set([m for m in matches if m not in builtins]))
        
        return dependencies


# 扩展：Java语言专用处理器
class JavaASTProcessor(ASTCodeProcessor):
    def __init__(self):
        super().__init__("java")
        
        if TREE_SITTER_AVAILABLE and self.language_obj:
            # 重写函数查询以支持Java的方法声明
            self.function_query = self.language_obj.query("""
                (method_declaration) @method
                (constructor_declaration) @constructor
            """)


# 扩展：JavaScript语言专用处理器
class JavaScriptASTProcessor(ASTCodeProcessor):
    def __init__(self):
        super().__init__("javascript")
        
        if TREE_SITTER_AVAILABLE and self.language_obj:
            # 支持箭头函数和函数表达式
            self.function_query = self.language_obj.query("""
                (function_declaration) @function
                (arrow_function) @arrow_function
                (function_expression) @function_expression
            """)


# ==================== 测试用例 ====================
def test_ast_processor():
    """测试AST处理器功能"""
    test_code = '''def add(a, b):
    """Add two numbers"""
    return a + b

def multiply(a, b):
    """Multiply two numbers"""
    return a * b

result = add(2, 3)
'''
    
    processor = ASTCodeProcessor("python")
    
    # 测试获取所有函数
    funcs = processor.get_all_functions(test_code)
    print("[OK] Found %d functions" % len(funcs))
    for f in funcs:
        print("  - %s: lines %d-%d" % (f.name, f.start_point[0]+1, f.end_point[0]+1))
    
    # 测试按名称查找函数
    func_info = processor.find_function_by_name(test_code, "add")
    if func_info:
        print("[OK] Found function 'add', start byte: %d" % func_info.start_byte)
    
    # 测试函数替换
    new_add_code = '''def add(a, b):
    """Add two numbers - fixed"""
    result = a + b
    return result
'''
    try:
        modified = processor.replace_function(test_code, "add", new_add_code)
        print("[OK] Function replacement successful")
        # 验证替换是否正确
        assert "Add two numbers - fixed" in modified
        print("[OK] Replacement content verified")
    except Exception as e:
        print("[FAIL] Function replacement test failed: %s" % str(e))
    
    # 测试导入修复
    code_without_imports = '''def process_data(data):
    return json.dumps(data)
'''
    fixed = processor.fix_imports(code_without_imports, ["import json"])
    if "import json" in fixed:
        print("[OK] Import fixing successful")
    
    print("\n[DONE] All AST processor tests completed!")


if __name__ == "__main__":
    test_ast_processor()


# ==================== 跨文件函数调用图溯源 ====================

class CallGraphTracer:
    """
    跨文件函数调用图溯源器。

    解决的核心问题：
    - pytest 报错说 X 函数有问题，但 bug 可能在 X 的调用者
    - 需要反向追传染链：谁调用了 X？谁又调用了那个调用者？

    使用方法：
        tracer = CallGraphTracer(repo_path)
        tracer.build_index()  # 构建索引（项目启动时做一次）
        # 报错时：
        trace = tracer.trace_back("iter_slices", max_depth=3)
        # 输出传染链
    """

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        self.processor = ASTCodeProcessor("python")
        self.index: Dict[str, Dict] = {}
        # {caller_func: [callee_func, ...]}
        # {callee_func: [(file, caller_func), ...]}

    def build_index(self) -> None:
        """
        构建整个仓库的函数调用图索引。

        遍历所有 .py 文件，对每个文件：
        1. 找所有函数定义
        2. 找所有函数调用
        3. 建立 caller→callee 和 callee→caller 双向索引
        """
        self.index = {
            "callers": {},   # {callee: [(file, caller_func), ...]}
            "definitions": {}, # {func_name: (file, start_byte, end_byte)}
        }

        for py_file in self.repo_path.rglob("*.py"):
            # 跳过 __pycache__ 和测试文件里的 fixture
            if "__pycache__" in str(py_file) or py_file.name.startswith("test_"):
                continue

            try:
                with open(py_file, "r", encoding="utf-8") as f:
                    code = f.read()
            except (UnicodeDecodeError, OSError):
                continue

            # 找所有函数定义
            funcs = self.processor.get_all_functions(code)
            for func in funcs:
                rel_path = str(py_file.relative_to(self.repo_path))
                key = f"{rel_path}::{func.name}"
                self.index["definitions"][func.name] = {
                    "file": rel_path,
                    "start_byte": func.start_byte,
                    "end_byte": func.end_byte,
                    "full_key": key,
                }

            # 找所有函数调用（当前文件内的调用）
            deps = self.processor.get_dependencies(code, funcs[0] if funcs else None)
            for called_func in deps.get("calls", []):
                for caller_func in funcs:
                    if caller_func.name not in self.index["callers"]:
                        self.index["callers"][caller_func.name] = []
                    self.index["callers"][caller_func.name].append({
                        "called": called_func,
                        "file": str(py_file.relative_to(self.repo_path)),
                    })

    def trace_back(self, func_name: str, max_depth: int = 3) -> List[Dict]:
        """
        反向追踪传染链：从报错函数出发，向上追调用者。

        Args:
            func_name: 出错的函数名
            max_depth: 最大追几步（防止无限循环）

        Returns:
            传染链列表，每项包含：depth, func_name, file, reason
        """
        visited = set()
        trace = []
        current_funcs = {func_name}
        depth = 0

        while current_funcs and depth < max_depth:
            next_funcs = set()
            for func in current_funcs:
                if func in visited:
                    continue
                visited.add(func)

                # 找谁调用了这个函数
                for caller_func, calls in self.index.get("callers", {}).items():
                    for call_info in calls:
                        if call_info["called"] == func:
                            def_info = self.index["definitions"].get(caller_func, {})
                            trace.append({
                                "depth": depth + 1,
                                "func_name": caller_func,
                                "file": def_info.get("file", "unknown"),
                                "called_from": func,
                                "reason": f"调用了 {func}",
                            })
                            next_funcs.add(caller_func)

            current_funcs = next_funcs
            depth += 1

        return trace

    def suggest_fix_target(self, func_name: str) -> str:
        """
        根据传染链判断修复目标：是被调用函数本身，还是上游调用者。

        规则：
        - 如果传染链为空 → bug 在当前函数本身
        - 如果传染链只有一个 → bug 可能在上游调用者（传了错误参数）
        - 如果传染链有多层 → 需要结合 pytest 错误信息判断
        """
        trace = self.trace_back(func_name, max_depth=2)

        if not trace:
            return f"FIX_TARGET: {func_name} (当前函数，传染链为空)"
        if len(trace) == 1:
            caller = trace[0]
            return (f"FIX_TARGET: {caller['func_name']} (上游调用者，depth={caller['depth']})"
                    f"，因为 {func_name} 被 {caller['func_name']} 调用，可能是传参错误")
        return (f"FIX_TARGET: 需进一步分析。传染链 {len(trace)} 个候选，"
                f"建议结合 pytest 错误信息缩小范围")
