#!/usr/bin/env python3
import os

root = r"c:\Users\Saixx\Desktop\collectorcrypt deal finder"
exclude_dirs = {'.venv', '__pycache__', '.git', '.pytest_cache', '.vscode'}
exclude_exts = {'.pyc', '.sqlite', '.db', '.db-shm', '.db-wal', '.log'}

def show_tree(path, prefix="", is_last=True):
    try:
        items = sorted(
            [item for item in os.listdir(path) 
             if item not in exclude_dirs and os.path.splitext(item)[1] not in exclude_exts],
            key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower())
        )
    except PermissionError:
        return
    
    for i, item in enumerate(items):
        is_last_item = (i == len(items) - 1)
        item_path = os.path.join(path, item)
        
        connector = "└── " if is_last_item else "├── "
        print(prefix + connector + item)
        
        if os.path.isdir(item_path) and item not in exclude_dirs:
            extension = "    " if is_last_item else "│   "
            show_tree(item_path, prefix + extension, is_last_item)

print("collectorcrypt deal finder/")
show_tree(root)
