# CodeIntel Agent Contract

A concise guide for agents using the CodeIntel DB. Intended to replace large
chunks of `AGENTS.md` later.

## 1. Purpose

CodeIntel is an **orientation and freshness layer**, not a replacement for
reading source before editing. It helps agents locate code quickly and judge
whether stored facts are current. It does not model program behaviour.

## 2. Standard agent workflow

Before doing broad source-file reads, agents should normally run:

```
python codeintel.py status
python codeintel.py stale
python codeintel.py orient <name>
python codeintel.py drifts
```

## 3. What agents may trust when the DB is fresh

When `stale` reports current, agents may trust these from CodeIntel:

- `qualified_name`
- `entity_type`
- `file_path`
- `start_line` / `end_line`
- `language`
- `detection_method`
- `signature`
- `docstring`
- open drifts

## 4. What agents must not assume from CodeIntel alone

- full implementation logic
- call graph
- imports
- side effects
- dead-code status
- behavioural correctness
- current source when stale / modified / missing

## 5. When agents must read source

- before editing
- when `stale` reports modified or missing
- when extraction failed
- when reasoning about control flow, imports, side effects, or behaviour
- when CodeIntel output is ambiguous

## 6. Token-saving rule

Use CodeIntel to **narrow** source reads, not to **eliminate** source reads
entirely.

## 7. Maintenance rule

After changing `codeintel.py`, run:

```
python codeintel_confidence.py
```

After changing repo source files, run:

```
python codeintel.py scan .
```

## 8. Git policy

Commit this contract file to git.

Do not commit generated runtime databases by default:

```
.codeintel/codeintel.sqlite
```

## 9. Path model

`git_repo.root_path` is the root used for all repo-relative paths.

`source_file.file_path` is the stable relative path used for entity lookup,
stale checks, and vanished-file detection.

| Scan type | root_path | source_file.file_path |
|---|---|---|
| Git repo root | git root | `relative/to/git/root.py` |
| Git subdirectory | git root | `subdir/file.py` (relative to git root, not subdir) |
| Single git file | git root | `path/to/file.py` (relative to git root) |
| Non-git directory | scan directory | `file.py` (relative to scan directory) |
| Non-git single file | file's parent directory | `file.py` |

Subdirectory scans only check files under that subdirectory's prefix for
vanished-file detection.  Files outside the prefix are never marked removed
by a subdirectory scan.
