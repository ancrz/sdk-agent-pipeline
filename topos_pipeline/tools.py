"""
tools.py — Local system access tools for Topos Pipeline Agents.

These functions are injected into the Google ADK Agents to give them 
"Computer Use" or "Agentic" capabilities, allowing them to read/write files 
and execute shell commands on the host machine.
"""

import os
import subprocess
from pathlib import Path


def run_shell_command(command: str) -> str:
    """Run a shell command on the local machine and return its output.
    
    Use this to interact with the OS, run git commands, install packages,
    or execute scripts.
    
    Args:
        command: The shell command to execute.
    
    Returns:
        The standard output of the command, or the error message if it fails.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode == 0:
            return result.stdout if result.stdout else "Command completed successfully (no output)."
        else:
            return f"Error (Exit code {result.returncode}):\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 120 seconds."
    except Exception as e:
        return f"Exception executing command: {str(e)}"


def read_file(path: str) -> str:
    """Read the contents of a local file.
    
    Args:
        path: The absolute or relative path to the file.
    
    Returns:
        The text content of the file.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file {path}: {str(e)}"


def write_file(path: str, content: str) -> str:
    """Write/overwrite content to a local file. 
    Creates parent directories if they do not exist.
    
    Args:
        path: The absolute or relative path to the file.
        content: The text content to write.
        
    Returns:
        A success or error message.
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote to {path}"
    except Exception as e:
        return f"Error writing file {path}: {str(e)}"


def list_directory(path: str = ".") -> str:
    """List the contents of a directory.
    
    Args:
        path: The directory path to list (defaults to current directory).
        
    Returns:
        A string containing the newline-separated list of files/folders.
    """
    try:
        items = os.listdir(path)
        if not items:
            return f"Directory {path} is empty."
        return "\n".join(sorted(items))
    except Exception as e:
        return f"Error listing directory {path}: {str(e)}"
