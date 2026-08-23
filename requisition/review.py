"""
Static review of the requisition module set.

Checks three things:
  1. Layer discipline  — no module imports one above its level
  2. Cross-module refs — every module.function() call resolves
  3. Internal calls    — every bare call() resolves to something in scope
"""
import ast, sys, builtins
from pathlib import Path

MODULES = ['config', 'dao', 'notifications', 'workflow', 'app']

# Lower layers first. A module may import only what is listed for it.
ALLOWED_IMPORTS = {
    'config':        set(),
    'dao':           {'config'},
    'notifications': {'config', 'dao'},
    'workflow':      {'config', 'dao', 'notifications'},
    'app':           {'workflow', 'dao'},
}

BUILTIN_NAMES = set(dir(builtins))


def collect_module_names(tree, module_name):
    """Every name a module defines at top level: functions, classes, constants."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def collect_scope_names(tree):
    """Names callable inside a module: its own definitions, imports, and the
    parameters of whichever function the call sits in."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)          # function parameters are callable
    return names


def check_imports(tree, module_name, errors):
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names if a.name in MODULES}
        elif isinstance(node, ast.ImportFrom) and node.module in MODULES:
            imported.add(node.module)
    illegal = imported - ALLOWED_IMPORTS[module_name]
    if illegal:
        errors.append(f"LAYER VIOLATION: {module_name}.py imports {illegal}")


def check_cross_module_calls(tree, module_name, defined, errors):
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            target = node.value.id
            if target in MODULES and target != module_name:
                if node.attr not in defined[target]:
                    errors.append(
                        f"UNRESOLVED: {module_name}.py -> {target}.{node.attr}"
                    )


def check_internal_calls(tree, module_name, errors):
    in_scope = collect_scope_names(tree) | BUILTIN_NAMES | set(MODULES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in in_scope:
                errors.append(
                    f"UNDEFINED CALL: {module_name}.py calls {node.func.id}()"
                )


def main():
    trees   = {m: ast.parse(Path(f'{m}.py').read_text()) for m in MODULES}
    defined = {m: collect_module_names(trees[m], m) for m in MODULES}
    errors  = []

    for m in MODULES:
        check_imports(trees[m], m, errors)
        check_cross_module_calls(trees[m], m, defined, errors)
        check_internal_calls(trees[m], m, errors)

    print("=" * 62)
    print("STATIC CODE REVIEW")
    print("=" * 62)
    for m in MODULES:
        print(f"  {m + '.py':20} {len(defined[m]):3} definitions")
    print("-" * 62)

    if errors:
        print(f"\n{len(set(errors))} issue(s):\n")
        for e in sorted(set(errors)):
            print(f"  ✗ {e}")
        return 1

    print("\n  ✓ Layer discipline clean")
    print("  ✓ All cross-module references resolve")
    print("  ✓ All internal calls resolve")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
