import os
import fnmatch

# Absolute paths to exclude (resolved at runtime relative to startpath)
EXCLUDE_PATHS = {
    '.pytest_cache',
    '.venv',
    'tests',
    'frontend/node_modules',
    'frontend/dist',
}

# Individual files to exclude (relative to startpath)
EXCLUDE_FILE_PATHS = {
    '.env',
    '.env.example',
    'generate_docs.py',
}

# Generic directory/file exclusions (applied everywhere)
EXCLUDE_DIRS = {'.git', '__pycache__', 'venv', 'build'}
EXCLUDE_FILE_PATTERNS = {'*.pyc', '*.pyo', '*.pyd', '.DS_Store', '*.txt'}


def _norm(p):
    """Normalize a path to forward slashes for consistent comparison."""
    return p.replace(os.sep, '/')


def should_exclude_dir(rel_dir_path):
    """Check if a directory should be excluded."""
    name = os.path.basename(rel_dir_path)
    if name in EXCLUDE_DIRS:
        return True
    normed = _norm(rel_dir_path)
    for ep in EXCLUDE_PATHS:
        if normed == ep or normed.startswith(ep + '/'):
            return True
    return False


def should_exclude_file(rel_file_path):
    """Check if a file should be excluded."""
    name = os.path.basename(rel_file_path)
    normed = _norm(rel_file_path)
    if normed in EXCLUDE_FILE_PATHS:
        return True
    for pattern in EXCLUDE_FILE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def generate_tree(startpath, base_rel=''):
    """Generate a directory tree string for a given path."""
    tree_str = "=== DIRECTORY STRUCTURE ===\n\n"
    for root, dirs, files in os.walk(startpath):
        rel_root = os.path.relpath(root, startpath)
        if rel_root == '.':
            full_rel = base_rel
        else:
            full_rel = os.path.join(base_rel, rel_root) if base_rel else rel_root

        dirs[:] = sorted([
            d for d in dirs
            if not should_exclude_dir(os.path.join(full_rel, d) if full_rel else d)
        ])

        level = 0 if rel_root == '.' else rel_root.count(os.sep) + 1
        indent = ' ' * 4 * level
        folder_name = os.path.basename(root) if rel_root != '.' else (base_rel or os.path.basename(startpath))
        tree_str += f"{indent}{folder_name}/\n"
        subindent = ' ' * 4 * (level + 1)
        for f in sorted(files):
            file_rel = os.path.join(full_rel, f) if full_rel else f
            if not should_exclude_file(file_rel):
                tree_str += f"{subindent}{f}\n"
    return tree_str


def write_file_docs(out, filepath, rel_path):
    """Write the source code and line-by-line explanation for a single file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        out.write(f"=== FILE: {_norm(rel_path)} ===\n")
        out.write("--- SOURCE CODE ---\n")
        out.write(content)
        if not content.endswith('\n'):
            out.write('\n')
        out.write("--- EXPLANATION ---\n")
        out.write(f"This is the source code for {_norm(rel_path)}.\n")
        lines = content.split('\n')
        for i, line in enumerate(lines, 1):
            if line.strip():
                out.write(f"Line {i}: {line.strip()}\n")
        out.write("\n\n")
    except UnicodeDecodeError:
        pass  # Skip binary files


def generate_docs_for_path(startpath, output_file, base_rel=''):
    """Generate a single documentation .txt for a subtree."""
    with open(output_file, 'w', encoding='utf-8') as out:
        out.write(generate_tree(startpath, base_rel))
        out.write("\n\n=== FILE CONTENTS AND EXPLANATIONS ===\n\n")

        for root, dirs, files in os.walk(startpath):
            rel_root = os.path.relpath(root, startpath)
            if rel_root == '.':
                full_rel = base_rel
            else:
                full_rel = os.path.join(base_rel, rel_root) if base_rel else rel_root

            dirs[:] = sorted([
                d for d in dirs
                if not should_exclude_dir(os.path.join(full_rel, d) if full_rel else d)
            ])

            for file in sorted(files):
                file_rel = os.path.join(full_rel, file) if full_rel else file
                if should_exclude_file(file_rel):
                    continue
                filepath = os.path.join(root, file)
                write_file_docs(out, filepath, file_rel)


def generate_all_docs(startpath, output_dir=None):
    """Generate per-subfolder .txt files and a root.txt for root-level files."""
    if output_dir is None:
        output_dir = startpath
    os.makedirs(output_dir, exist_ok=True)

    root_files = []
    subdirs = []

    for entry in sorted(os.listdir(startpath)):
        entry_path = os.path.join(startpath, entry)
        if os.path.isdir(entry_path):
            if not should_exclude_dir(entry):
                subdirs.append(entry)
        else:
            if not should_exclude_file(entry):
                root_files.append(entry)

    # Generate root.txt for files in the root directory
    if root_files:
        root_output = os.path.join(output_dir, 'root.txt')
        with open(root_output, 'w', encoding='utf-8') as out:
            out.write("=== ROOT-LEVEL FILES ===\n\n")
            out.write("=== FILE CONTENTS AND EXPLANATIONS ===\n\n")
            for file in root_files:
                filepath = os.path.join(startpath, file)
                write_file_docs(out, filepath, file)
        print(f"Generated: root.txt")

    # Generate one .txt per top-level subfolder
    for subdir in subdirs:
        subdir_path = os.path.join(startpath, subdir)
        output_file = os.path.join(output_dir, f'{subdir}.txt')
        generate_docs_for_path(subdir_path, output_file, base_rel=subdir)
        print(f"Generated: {subdir}.txt")


if __name__ == '__main__':
    generate_all_docs('.')
