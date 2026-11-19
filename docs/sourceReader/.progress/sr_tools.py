#!/usr/bin/env python3
"""sr_tools.py — sourceReader 定位与校验工具（DeepSeek Harness TS 仓库版）。

子命令：
  locate SYM...          全库定位符号定义（先生产树 packages/ apps/，未命中再扫 tests/scripts/python）
  outline FILE [--top N] 输出文件符号骨架（类/函数/常量/方法）+ JSDoc 首行
  offset FILE SYM...     输出符号定义行号（+0 行；偏移 = 目标行 − 定义行）
  verify DOC...          校验文档里的文件路径引用，分四档报告
  symbols DOC...         校验 `path` · `sym` / 路径后括号符号 的符号归属

记法约定：`+0` = 符号定义行，`+N` = 符号体内第 N 行（闭区间）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "docs/sourceReader"

PASS1_ROOTS = ["packages", "apps"]
PASS2_ROOTS = ["packages", "apps", "scripts", "python"]
SKIP_DIRS = {".git", "node_modules", "lib", "dist", "coverage", ".sessions", ".storages", "__pycache__", ".progress", ".generated", ".dist"}


CLASS_RE = re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)")
FN_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s+(\w+)")
CONST_RE = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)")
TYPE_RE = re.compile(r"^\s*(?:export\s+)?(?:type|interface|enum)\s+(\w+)")
METHOD_RE = re.compile(r"^\s+(?:(?:public|private|protected|static|override|async|get|set)\s+|\*)*(\w+)\s*(?:<[^>{]*>)?\s*(?:\(|=)")
METHOD_NOISE = {"if", "for", "while", "switch", "catch", "return", "else", "do", "try", "new", "typeof", "await", "yield", "case", "throw", "constructor"}


def method_or_none(m):
    """METHOD_RE 命名组过滤控制流关键字；命中返回名字，否则 None。"""
    name = m.group(1)
    return None if name in METHOD_NOISE else name


def iter_ts_files():
    """两遍扫描：第一遍只扫生产树（*/src/**），第二遍补 tests/scripts/python 兜底。"""
    for pass_no, src_only in ((1, True), (2, False)):
        seen = set()
        for base in PASS2_ROOTS:
            base_path = ROOT / base
            if not base_path.exists():
                continue
            for p in base_path.rglob("*"):
                if not p.is_file() or p.suffix not in (".ts", ".tsx", ".mts", ".cts", ".py"):
                    continue
                if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
                    continue
                rp = p.relative_to(ROOT).as_posix()
                if rp in seen:
                    continue
                is_src = "/src/" in f"/{rp}"
                if src_only != is_src:
                    continue
                seen.add(rp)
                yield pass_no, rp


def build_index():
    """symbol -> [(file, line, kind)]；两遍扫描：生产树优先，测试/脚本兜底。"""
    index: dict[str, list] = {}
    for pass_no, rp in iter_ts_files():
        try:
            text = (ROOT / rp).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for rx, kind in ((CLASS_RE, "class"), (FN_RE, "fn"), (CONST_RE, "const"), (TYPE_RE, "type"), (METHOD_RE, "method")):
                m = rx.match(line)
                if m:
                    name = method_or_none(m) if rx is METHOD_RE else m.group(1)
                    if name:
                        index.setdefault(name, []).append((rp, lineno, kind, pass_no))
                    break
    return index


INDEX = None


def get_index():
    global INDEX
    if INDEX is None:
        INDEX = build_index()
    return INDEX


def find_lines(path: Path):
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def sym_def_line(lines, sym):
    """在单文件内找符号定义行（+0）。按 class > fn > type > 方法(带括号) > 常量 优先，
    避免方法体内的局部 const 抢占方法名（实测 throwError 的 const turn 曾误报）。"""
    pats = [
        re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+" + re.escape(sym) + r"\b"),
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s+" + re.escape(sym) + r"\b"),
        re.compile(r"^\s*(?:export\s+)?(?:type|interface|enum)\s+" + re.escape(sym) + r"\b"),
        re.compile(r"^\s+(?:(?:public|private|protected|static|override|async|get|set)\s+|\*)*" + re.escape(sym) + r"(?:\s*<[^>{]*>)?\s*\("),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+" + re.escape(sym) + r"\b"),
        re.compile(r"^\s+(?:(?:public|private|protected|static|readonly|override|async|get|set)\s+|\*)*" + re.escape(sym) + r"\s*="),
    ]
    for rx in pats:
        for lineno, line in enumerate(lines, 1):
            if rx.match(line):
                return lineno
    return None


def cmd_locate(syms):
    index = get_index()
    for sym in syms:
        hits = index.get(sym, [])
        prod = [h for h in hits if h[3] == 1]
        fallback = [h for h in hits if h[3] == 2]
        if not hits:
            print(f"✗ {sym}: 未找到（两遍扫描后）")
            continue
        for rp, line, kind, _ in prod[:8]:
            print(f"{sym}: {rp}:{line} ({kind})")
        for rp, line, kind, _ in fallback[:4]:
            print(f"{sym}: {rp}:{line} ({kind}, 测试/脚本兜底)")


def cmd_outline(path_str, top=None):
    p = ROOT / path_str
    lines = find_lines(p)
    if not lines:
        print(f"✗ 文件不存在或为空: {path_str}")
        return
    shown = 0
    for lineno, line in enumerate(lines, 1):
        for rx in (CLASS_RE, FN_RE, CONST_RE, TYPE_RE, METHOD_RE):
            m = rx.match(line)
            if m and (rx is not METHOD_RE or method_or_none(m)):
                doc = ""
                for back in range(lineno - 2, max(lineno - 5, 0), -1):
                    s = lines[back - 1].strip()
                    if s.endswith("*/"):
                        for up in range(back - 1, max(lineno - 12, 0), -1):
                            s2 = lines[up - 1].strip()
                            if s2.startswith("/*") or s2.startswith("*"):
                                continue
                            doc = s2
                            break
                        break
                    if s.startswith("//"):
                        doc = s.lstrip("/").strip()
                        break
                    if s and not s.startswith(("@", "*", "/*")):
                        break
                print(f"{lineno:5d}  {m.group(1):40s} {doc[:80]}")
                shown += 1
                if top and shown >= top:
                    return
                break


def cmd_offset(path_str, syms):
    lines = find_lines(ROOT / path_str)
    if not lines:
        print(f"✗ 文件不存在: {path_str}")
        return
    for sym in syms:
        base = sym.split(".")[-1]
        line = sym_def_line(lines, base)
        print(f"{path_str} · {sym}: +0 = L{line}" if line else f"✗ {path_str} · {sym}: 未找到")


# ---------- verify：文件路径引用 ----------

PATH_TOKEN = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:tsx|ts|mts|cts|py|json|ya?ml|md))`")
NEGATIVE = ("不存在", "已删除", "已移除", "已废弃", "已下线", "不再", "no longer", "removed", "deleted", "已改名", "已迁移")
CONFIG_NAMES = {"config.yaml", "cordis.yml", "package.json", "tsconfig.json", "config.yml", "settings.json", ".env"}


def resolve_doc_path(token, doc):
    bases = [ROOT, doc.parent, *doc.parents[:-1]]
    for base in bases:
        cand = (base / token).resolve()
        if cand.is_file():
            return cand
    return None


def cmd_verify(docs):
    bad = neg = configname = bare = ok = 0
    for doc in docs:
        d = (ROOT / doc) if not doc.startswith("/") else Path(doc)
        if d.is_dir():
            d = sorted(list(d.glob("*.md")) + list(d.rglob("*.md")))
        else:
            d = [d]
        for md in d:
            for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
                for m in PATH_TOKEN.finditer(line):
                    token = m.group(1)
                    if resolve_doc_path(token, md):
                        ok += 1
                        continue
                    if token in CONFIG_NAMES:
                        configname += 1
                        print(f"· 配置名: {md.relative_to(ROOT)}:{lineno} `{token}`")
                    elif "/" not in token and any(p.name == token for p in ROOT.rglob(token)):
                        bare += 1
                        print(f"· 裸名缩写: {md.relative_to(ROOT)}:{lineno} `{token}`")
                    elif any(w in line for w in NEGATIVE):
                        neg += 1
                        print(f"· 否定语境: {md.relative_to(ROOT)}:{lineno} `{token}`")
                    else:
                        bad += 1
                        print(f"✗ 疑似失效: {md.relative_to(ROOT)}:{lineno} `{token}`")
    print(f"\nverify 汇总: OK {ok} | ✗ 疑似失效 {bad} | · 否定 {neg} | · 配置名 {configname} | · 裸名 {bare}")


# ---------- symbols：符号归属校验 ----------

PAIR = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:tsx|ts|mts|cts))`\s*[·（(]\s*`?([A-Za-z_][A-Za-z0-9_.]*)`?")
AFTER_PATH = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:tsx|ts|mts|cts))`[^`]*?`([A-Za-z_][A-Za-z0-9_.]*)`")
SKIP_SYMS = {"config", "type", "const", "function", "class", "module", "export", "default", "return", "import", "string", "boolean", "number", "promise", "void", "true", "false", "null", "undefined", "this", "super"}


def cmd_symbols(docs):
    ok = bad = 0
    for doc in docs:
        d = (ROOT / doc) if not doc.startswith("/") else Path(doc)
        files = sorted(d.glob("*.md")) + sorted(d.rglob("*.md")) if d.is_dir() else [d]
        for md in files:
            for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
                seen_here = set()
                for m in PAIR.finditer(line):
                    token, sym = m.group(1), m.group(2)
                    if sym.lower() in SKIP_SYMS or (token, sym) in seen_here:
                        continue
                    seen_here.add((token, sym))
                    target = resolve_doc_path(token, md)
                    if target is None:
                        for base in [md.parent, *md.parents[:-1]]:
                            cand = (base / token).resolve()
                            if cand.is_file():
                                target = cand
                                break
                    if target is None:
                        continue
                    lines = find_lines(target)
                    base_sym = sym.split(".")[-1]
                    if sym_def_line(lines, base_sym):
                        ok += 1
                    else:
                        bad += 1
                        print(f"✗ 符号未归属: {md.relative_to(ROOT)}:{lineno} `{token}` · `{sym}`")
    print(f"\nsymbols 汇总: OK {ok} | ✗ 未归属 {bad}")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]
    if cmd == "locate":
        cmd_locate(rest)
    elif cmd == "outline":
        top = None
        if "--top" in rest:
            i = rest.index("--top")
            top = int(rest[i + 1])
            rest = rest[:i] + rest[i + 2:]
        cmd_outline(rest[0], top)
    elif cmd == "offset":
        cmd_offset(rest[0], rest[1:])
    elif cmd == "verify":
        cmd_verify(rest)
    elif cmd == "symbols":
        cmd_symbols(rest)
    else:
        print(f"未知子命令: {cmd}")
        sys.exit(2)


if __name__ == "__main__":
    main()
