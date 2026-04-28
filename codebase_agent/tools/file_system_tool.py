import os
import re
import logging
from pathlib import Path
from typing import Dict, Any, Tuple

logger = logging.getLogger(__name__)

class FileSystemTool:
    """Secure native Python filesystem interaction tool for codebase exploration."""
    
    def __init__(self, working_directory: str, max_output_size: int = 15000):
        self.working_directory = Path(working_directory).resolve()
        self.max_output_size = max_output_size
        
        if not self.working_directory.exists() or not self.working_directory.is_dir():
            raise ValueError(f"Invalid working directory: {self.working_directory}")

    def execute_operation(self, action: str, arguments: Dict[str, Any]) -> Tuple[bool, str, str]:
        """
        Execute a requested file operation based on the action name.
        Returns: (success_bool, stdout, stderr)
        """
        try:
            if action == "list_directory":
                return True, self.list_directory(arguments.get("path", ".")), ""
            elif action == "read_file":
                start_line = arguments.get("start_line", 1)
                max_lines = arguments.get("max_lines", 300)
                return True, self.read_file(arguments.get("path", ""), start_line, max_lines), ""
            elif action == "search_content":
                return True, self.search_content(arguments.get("query", ""), arguments.get("path", ".")), ""
            else:
                return False, "", f"Unknown action: {action}"
        except Exception as e:
            return False, "", str(e)
            
    def _resolve_safe_path(self, target_path: str) -> Path:
        """Resolve a path and ensure it doesn't escape the working directory."""
        if not target_path:
            target_path = "."
        resolved = (self.working_directory / target_path).resolve()
        if not str(resolved).startswith(str(self.working_directory)):
            raise PermissionError(f"Path access denied: {target_path} restricts to {self.working_directory}")
        return resolved

    def list_directory(self, path: str) -> str:
        """List contents of a directory natively."""
        try:
            target = self._resolve_safe_path(path)
            if not target.exists():
                return f"Path does not exist: {path}"
            if not target.is_dir():
                return f"{path} is a file, not a directory."
                
            output = [f"Directory contents of {path}:"]
            for item in target.iterdir():
                size = item.stat().st_size if item.is_file() else "DIR"
                output.append(f"{size:>10}  {item.name}")
                
            res = "\n".join(output)
            if len(res) > self.max_output_size:
                return res[:self.max_output_size] + "\n...[truncated]"
            return res or "Empty directory"
            
        except PermissionError as e:
            return str(e)
        except Exception as e:
            return f"Failed to list directory: {e}"

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 300) -> str:
        """Read text lines from a file natively."""
        try:
            target = self._resolve_safe_path(path)
            if not target.exists() or not target.is_file():
                return f"File does not exist or is not a file: {path}"
            
            # Binary check
            with open(target, 'rb') as test_f:
                chunk = test_f.read(1024)
                if b'\0' in chunk:
                    return f"Binary file detected: {path}. Cannot read text."
                    
            with open(target, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
            total = len(lines)
            idx_start = max(0, start_line - 1)
            idx_end = min(total, idx_start + max_lines)
            
            snippet = "".join([f"{i+1:4d} | {lines[i]}" for i in range(idx_start, idx_end)])
            return f"File: {path} (Lines {idx_start+1}-{idx_end} of {total})\n{snippet}"
            
        except UnicodeDecodeError:
            return f"Error decoding file {path} (likely binary or non-utf8)."
        except PermissionError as e:
            return str(e)
        except Exception as e:
            return f"Failed to read {path}: {e}"

    def search_content(self, query: str, path: str) -> str:
        """Search contents using Python regex."""
        if not query:
            return "Query cannot be empty"
        
        try:
            target = self._resolve_safe_path(path)
            if not target.exists():
                return f"Path does not exist: {path}"
            
            output = []
            regex = re.compile(query, re.IGNORECASE)
            
            if target.is_file():
                self._search_single_file(target, regex, output, target)
            else:
                for root, _, files in os.walk(target):
                    for file in files:
                        file_path = Path(root) / file
                        # Skip typical binary or generated dirs to prevent extreme lag
                        if any(skip in str(file_path) for skip in ['.git', '__pycache__', 'node_modules', '.venv', '.env']):
                            continue
                        self._search_single_file(file_path, regex, output, target)
                        
                        if len("\n".join(output)) > self.max_output_size:
                            break
                    if len("\n".join(output)) > self.max_output_size:
                        break
                        
            res = "\n".join(output)
            if len(res) > self.max_output_size:
                return res[:self.max_output_size] + "\n...[Output truncated]"
            return res or "No matches found."
            
        except re.error as e:
            return f"Invalid regex query '{query}': {e}"
        except Exception as e:
            return f"Search error: {e}"
            
    def _search_single_file(self, file_path: Path, regex: re.Pattern, output: list, base_path: Path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for i, line in enumerate(f, 1):
                    if regex.search(line):
                        rel_path = file_path.relative_to(base_path) if base_path.is_dir() else file_path.name
                        output.append(f"{rel_path}:{i}: {line.strip()}")
        except (UnicodeDecodeError, PermissionError):
            pass # Skip binaries and unreadable files
