def collect_repo_metrics(repo_path="."):
    import os

    python_files = []
    total_lines = 0

    for root, dirs, files in os.walk(repo_path):
        for f in files:
            if f.endswith(".py"):
                filepath = os.path.join(root, f)
                python_files.append(filepath)
                with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                    total_lines += sum(1 for _ in fh.readlines())

    return {
        "status": "OK",
        "python_files_count": len(python_files),
        "total_lines": total_lines,
    }