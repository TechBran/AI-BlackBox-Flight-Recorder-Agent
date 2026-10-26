#!/usr/bin/env python3
"""
Generate Embeddings & Dependencies for Code Manifest

This script:
1. Loads code_manifest.json
2. Extracts each section using byte offsets
3. Generates semantic embeddings (the active embedding model)
4. Analyzes dependencies (imports, function calls, globals)
5. Outputs code_embeddings.json for semantic search

Usage:
    python3 scripts/generate_embeddings.py

Output:
    Manifest/code_embeddings.json
    Manifest/code_dependencies.json
"""

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import from Orchestrator (reuse existing embedding function: the active
# embedding provider, which carries its own key)
try:
    from Orchestrator.app import generate_embedding
    EMBEDDINGS_AVAILABLE = True
except Exception as e:
    print(f"⚠️  Warning: Could not import embedding function: {e}")
    print("   Embeddings will be skipped (can generate later)")
    EMBEDDINGS_AVAILABLE = False

# Paths
BLACKBOX_ROOT = Path(__file__).parent.parent
MANIFEST_PATH = BLACKBOX_ROOT / "Manifest" / "code_manifest.json"
EMBEDDINGS_OUTPUT = BLACKBOX_ROOT / "Manifest" / "code_embeddings.json"
DEPENDENCIES_OUTPUT = BLACKBOX_ROOT / "Manifest" / "code_dependencies.json"
APP_PY_PATH = BLACKBOX_ROOT / "Orchestrator" / "app.py"


def extract_section_code(source_bytes: bytes, byte_start: int, byte_end: int) -> str:
    """Extract section code using byte offsets."""
    section_bytes = source_bytes[byte_start:byte_end]
    return section_bytes.decode('utf-8')


def analyze_imports(code: str) -> Dict[str, List[str]]:
    """Extract import statements from code."""
    imports = {
        "stdlib": [],
        "external": [],
        "local": []
    }

    # Pattern: import X or from X import Y
    import_pattern = re.compile(r'^\s*(?:from\s+([^\s]+)\s+)?import\s+([^\s#]+)', re.MULTILINE)

    for match in import_pattern.finditer(code):
        module = match.group(1) or match.group(2)
        module = module.split('.')[0]  # Get top-level package

        # Categorize
        if module.startswith('.'):
            imports["local"].append(module)
        elif module in ['os', 'sys', 'json', 'time', 're', 'hashlib', 'threading',
                       'asyncio', 'subprocess', 'dataclasses', 'typing', 'pathlib',
                       'datetime', 'math', 'io', 'wave', 'sqlite3', 'uuid', 'base64']:
            imports["stdlib"].append(module)
        else:
            imports["external"].append(module)

    return {k: sorted(set(v)) for k, v in imports.items()}


def analyze_function_calls(code: str) -> List[str]:
    """Extract function calls (rough heuristic)."""
    # Pattern: function_name( with word characters/underscores
    pattern = re.compile(r'([a-z_][a-z0-9_]*)\s*\(', re.IGNORECASE)
    matches = pattern.findall(code)

    # Filter out common keywords
    keywords = {'if', 'for', 'while', 'def', 'class', 'return', 'print', 'len', 'str', 'int', 'dict', 'list', 'set'}
    functions = [m for m in matches if m not in keywords]

    # Return top 20 most common
    from collections import Counter
    counts = Counter(functions)
    return [func for func, count in counts.most_common(20)]


def analyze_global_accesses(code: str) -> List[str]:
    """Find potential global variable accesses (heuristic)."""
    # Pattern: UPPERCASE_VARS or common globals
    known_globals = [
        'CURRENT_OPERATOR', 'AGENT_SESSIONS', 'APP_REGISTRY',
        'state_by_op', 'task_db', 'task_queue', 'worker_running',
        'PERSISTED_AGENT_SESSIONS', 'SNAPSHOT_INDEX', 'VOL_PATH',
        'MANIFEST', 'CFG', 'mint_lock', 'state_lock', 'task_lock', 'agent_lock'
    ]

    found = []
    for global_var in known_globals:
        if re.search(r'\b' + re.escape(global_var) + r'\b', code):
            found.append(global_var)

    return sorted(found)


def analyze_routes(code: str) -> List[str]:
    """Extract route definitions."""
    routes = []

    # Pattern: @app.get("/path") or @app.post("/path") or @app.websocket("/path")
    pattern = re.compile(r'@app\.(get|post|put|delete|patch|websocket)\(["\']([^"\']+)["\']')

    for match in pattern.finditer(code):
        method = match.group(1).upper()
        path = match.group(2)
        routes.append(f"{method} {path}")

    return sorted(routes)


def generate_section_embedding(code: str, section_name: str) -> List[float]:
    """Generate embedding for code section."""
    if not EMBEDDINGS_AVAILABLE:
        return None

    print(f"   Generating embedding for {section_name}...")

    # Truncate to 10,000 chars (Gemini limit)
    text = code[:10000]

    try:
        embedding = generate_embedding(text)
        if embedding:
            print(f"   ✅ Embedding generated ({len(embedding)} dimensions)")
            return embedding
        else:
            print(f"   ⚠️  Embedding generation returned None")
            return None
    except Exception as e:
        print(f"   ❌ Error generating embedding: {e}")
        return None


def main():
    print("="*70)
    print("EMBEDDING & DEPENDENCY GENERATOR - Day 2")
    print("="*70)
    print()

    # Load manifest
    print(f"Loading manifest: {MANIFEST_PATH}")
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    print(f"Found {len(manifest['sections'])} sections to process\n")

    # Load source file
    source_bytes = APP_PY_PATH.read_bytes()

    embeddings = {}
    dependencies = {}

    # Process each section
    for section_name, section_info in manifest["sections"].items():
        print(f"Processing: {section_name}")
        print(f"  Lines {section_info['line_start']}-{section_info['line_end']}")

        # Extract code
        code = extract_section_code(
            source_bytes,
            section_info["byte_start"],
            section_info["byte_end"]
        )

        print(f"  Extracted {len(code)} characters")

        # Analyze dependencies
        imports = analyze_imports(code)
        functions = analyze_function_calls(code)
        globals_used = analyze_global_accesses(code)
        routes = analyze_routes(code)

        dependencies[section_name] = {
            "imports_stdlib": imports["stdlib"],
            "imports_external": imports["external"],
            "imports_local": imports["local"],
            "top_functions_called": functions,
            "global_vars_accessed": globals_used,
            "routes_defined": routes,
            "description": section_info["description"],
            "target_module": section_info["target_module"]
        }

        print(f"  Dependencies: {len(imports['stdlib'])} stdlib, {len(imports['external'])} external, {len(globals_used)} globals")
        print(f"  Routes: {len(routes)}")

        # Generate embedding
        if EMBEDDINGS_AVAILABLE:
            embedding = generate_section_embedding(code, section_name)
            embeddings[section_name] = {
                "embedding": embedding,
                "description": section_info["description"],
                "target_module": section_info["target_module"],
                "byte_range": [section_info["byte_start"], section_info["byte_end"]],
                "line_range": [section_info["line_start"], section_info["line_end"]]
            }
        else:
            embeddings[section_name] = {
                "embedding": None,
                "description": section_info["description"],
                "target_module": section_info["target_module"]
            }

        print()

    # Save embeddings
    print("="*70)
    print(f"Saving embeddings to: {EMBEDDINGS_OUTPUT}")
    with open(EMBEDDINGS_OUTPUT, 'w') as f:
        json.dump(embeddings, f, indent=2)

    embeddings_size = EMBEDDINGS_OUTPUT.stat().st_size
    print(f"✅ Saved {embeddings_size:,} bytes")

    # Save dependencies
    print(f"\nSaving dependencies to: {DEPENDENCIES_OUTPUT}")
    with open(DEPENDENCIES_OUTPUT, 'w') as f:
        json.dump(dependencies, f, indent=2)

    deps_size = DEPENDENCIES_OUTPUT.stat().st_size
    print(f"✅ Saved {deps_size:,} bytes")

    # Summary
    print("\n" + "="*70)
    print("DAY 2 COMPLETE - Embeddings & Dependencies Generated")
    print("="*70)
    print(f"Sections processed: {len(embeddings)}")
    print(f"Embeddings generated: {sum(1 for e in embeddings.values() if e['embedding'])}")
    print(f"Dependencies mapped: {len(dependencies)}")
    print()
    print("You can now:")
    print("  1. Semantic search: Query any section by meaning")
    print("  2. Dependency analysis: See what each section needs")
    print("  3. Impact assessment: Predict extraction complexity")
    print()
    print("Next: Day 3 - Build integration test harness")
    print("="*70)

    return 0


if __name__ == "__main__":
    exit(main())
