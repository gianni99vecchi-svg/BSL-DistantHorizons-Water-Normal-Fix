#!/usr/bin/env python3
"""
Apply the "Distant Horizons normal fix" to YOUR OWN copy of BSL Shaders v10.1.8.

This repository does not contain BSL Shaders. Download BSL from its official page, then run:

    python apply_patch.py path/to/BSL_v10.1.8.zip            # or a folder that contains "shaders/"
    python apply_patch.py path/to/BSL_v10.1.8.zip --out C:/some/where/BSL_v10.1.8_NORMAL-PATCH

The result is a NEW folder-pack (default: "<input>_NORMAL-PATCH" next to the input). The input is never
modified. The patched pack is for your personal use only: BSL is "all rights reserved" by its author.

Only the Python standard library is used (Python 3.8+).
"""
import argparse
import os
import shutil
import sys
import zipfile

SUPPORTED_VERSION_LINE = "option.ABOUT=BSL v10.1.8"

# The fix itself. It goes into the vertex shader of gbuffers_water, right after the TBN vectors are built.
FIX_BLOCK = [
    "",
    "\t#if WATER_NORMAL_FIX == 1",
    "\t// NORMAL-PATCH: with Distant Horizons rendering, translucent chunk geometry (water, glass) arrives with",
    "\t// inverted normals (water top face = (0,-1,0)). That sends fresnel to 1 (mirror) and kills the waves, because",
    "\t// their strength is 0.35 * (1 - fresnel). Re-orient the normal from the geometry instead of trusting its sign:",
    "\t// seen from outside, a face looks at the camera; water seen from inside the water looks away from it.",
    "\t{",
    "\t\tfloat facing = dot(normal, (gl_ModelViewMatrix * gl_Vertex).xyz); // > 0: normal points away from the camera",
    "\t\tbool waterFace = (blockID == 200 || blockID == 204 || blockID == 205);",
    "\t\tbool wantAway = waterFace && isEyeInWater == 1;",
    "\t\tif (wantAway ? facing < 0.0 : facing > 0.0) {",
    "\t\t\tnormal = -normal;",
    "\t\t\tbinormal = -binormal;",
    "\t\t}",
    "\t}",
    "\t#endif",
]

LANG_BLOCK = [
    "",
    "option.WATER_NORMAL_FIX=Normal Fix (DH)",
    "option.WATER_NORMAL_FIX.comment=Re-orients water and glass normals from the geometry. Fixes mirror-like flat "
    "water when Distant Horizons is rendering and the normals arrive inverted.",
    "value.WATER_NORMAL_FIX.0=OFF",
    "value.WATER_NORMAL_FIX.1=ON",
]

README_IN_PACK = """BSL v10.1.8 + Distant Horizons normal fix (NORMAL-PATCH)
=========================================================

This folder is BSL Shaders v10.1.8 by Capt Tatsu (https://capttatsu.com), modified for PERSONAL USE ONLY by a
patch script. Do not redistribute it. See the patch project for what changed and why.

Changed files:
  shaders/program/gbuffers_water.glsl   uniform isEyeInWater (vertex) + the fix block (#if WATER_NORMAL_FIX == 1)
  shaders/lib/settings.glsl             #define WATER_NORMAL_FIX 1 //[0 1]
  shaders/shaders.properties            WATER_NORMAL_FIX added to screen.WATER (3 columns x 5 rows)
  shaders/lang/en_US.lang               option label and description

In game: Shader options > Environment > Water > "Normal Fix (DH)" (ON by default; OFF = original BSL).
"""


class PatchError(Exception):
    pass


def find_pack_root_in_zip(names):
    """Return the prefix (possibly '') under which 'shaders/' lives inside the zip."""
    for n in names:
        if n == "shaders/" or n.startswith("shaders/"):
            return ""
    candidates = {n.split("/", 1)[0] for n in names if "/shaders/" in n}
    if len(candidates) == 1:
        return candidates.pop() + "/"
    raise PatchError("Could not find a 'shaders/' folder inside the zip.")


def extract_pack(src, dest):
    """Copy/extract the input pack into dest so that dest/shaders/... exists. dest must not exist."""
    if os.path.isdir(src):
        root = src
        if not os.path.isdir(os.path.join(root, "shaders")):
            subs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d, "shaders"))]
            if len(subs) != 1:
                raise PatchError("The folder does not contain 'shaders/'.")
            root = os.path.join(root, subs[0])
        shutil.copytree(root, dest)
        return
    if not zipfile.is_zipfile(src):
        raise PatchError("Input is neither a folder nor a zip file.")
    with zipfile.ZipFile(src) as z:
        prefix = find_pack_root_in_zip(z.namelist())
        dest_abs = os.path.abspath(dest)
        os.makedirs(dest_abs)
        for info in z.infolist():
            if not info.filename.startswith(prefix) or info.filename == prefix:
                continue
            rel = info.filename[len(prefix):]
            target = os.path.abspath(os.path.join(dest_abs, rel))
            if target != dest_abs and not target.startswith(dest_abs + os.sep):
                raise PatchError("Unsafe path in zip: " + info.filename)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as fin, open(target, "wb") as fout:
                    shutil.copyfileobj(fin, fout)


def read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", errors="surrogateescape")  # keeps unknown bytes byte-exact
    nl = "\r\n" if "\r\n" in text else "\n"
    return text.split(nl), nl


def write_text(path, lines, nl):
    with open(path, "wb") as f:
        f.write(nl.join(lines).encode("utf-8", errors="surrogateescape"))


def find_sequence(lines, seq):
    """Indices where the stripped lines match seq (consecutive). Must be exactly one."""
    hits = [i for i in range(len(lines) - len(seq) + 1)
            if all(lines[i + k].strip() == seq[k] for k in range(len(seq)))]
    if len(hits) != 1:
        raise PatchError("Anchor %r found %d times (expected 1). Is this really BSL v10.1.8?" % (seq, len(hits)))
    return hits[0]


def edit_gbuffers_water(shaders):
    path = os.path.join(shaders, "program", "gbuffers_water.glsl")
    lines, nl = read_text(path)
    if any("WATER_NORMAL_FIX" in l for l in lines):
        raise PatchError("Already patched.")
    # 1) vertex-shader uniform (the vertex shader has no isEyeInWater in v10.1.8)
    i = find_sequence(lines, ["//Uniforms//", "uniform int worldTime;", "", "uniform float far;"])
    lines.insert(i + 1, "uniform int isEyeInWater;")
    # 2) the fix block, right after the tangent vector
    j = find_sequence(lines, ["tangent  = normalize(gl_NormalMatrix * at_tangent.xyz);"])
    lines[j + 1:j + 1] = FIX_BLOCK
    write_text(path, lines, nl)


def edit_settings(shaders):
    path = os.path.join(shaders, "lib", "settings.glsl")
    lines, nl = read_text(path)
    i = find_sequence(lines, ["#define WATER_PIXEL 0 //[0 8 16 32 64 128]"])
    lines.insert(i + 1, "  #define WATER_NORMAL_FIX 1 //[0 1]")
    write_text(path, lines, nl)


def edit_properties(shaders):
    """Water screen: drop the <empty> spacers (15 options) and force 3 columns -> 3 x 5 grid."""
    path = os.path.join(shaders, "shaders.properties")
    lines, nl = read_text(path)
    idx = [i for i, l in enumerate(lines) if l.startswith("screen.WATER=")]
    if len(idx) != 1:
        raise PatchError("screen.WATER line not found exactly once.")
    i = idx[0]
    items = [t for t in lines[i].split("=", 1)[1].split() if t != "<empty>"]
    items.append("WATER_NORMAL_FIX")
    lines[i] = "screen.WATER=" + " ".join(items)
    lines.insert(i + 1, "screen.WATER.columns=3")
    write_text(path, lines, nl)


def edit_lang(shaders):
    path = os.path.join(shaders, "lang", "en_US.lang")
    lines, nl = read_text(path)
    i = find_sequence(lines, ["value.WATER_PIXEL.0=OFF"])
    lines[i + 1:i + 1] = LANG_BLOCK
    write_text(path, lines, nl)


def check_version(shaders):
    lines, _ = read_text(os.path.join(shaders, "lang", "en_US.lang"))
    if SUPPORTED_VERSION_LINE not in [l.strip() for l in lines]:
        raise PatchError("This script targets BSL v10.1.8 (expected the line %r in lang/en_US.lang). "
                         "Use --force to try anyway." % SUPPORTED_VERSION_LINE)


def main():
    ap = argparse.ArgumentParser(description="Apply the DH normal fix to your own copy of BSL v10.1.8.")
    ap.add_argument("input", help="BSL_v10.1.8.zip, or a folder containing 'shaders/'")
    ap.add_argument("--out", help="output folder-pack (default: <input without .zip>_NORMAL-PATCH)")
    ap.add_argument("--force", action="store_true", help="skip the BSL version check")
    args = ap.parse_args()

    src = os.path.abspath(args.input)
    if not os.path.exists(src):
        sys.exit("Input not found: " + src)
    base = src[:-4] if src.lower().endswith(".zip") else src
    out = os.path.abspath(args.out) if args.out else base + "_NORMAL-PATCH"
    if os.path.exists(out):
        sys.exit("Output already exists, refusing to overwrite: " + out)

    try:
        extract_pack(src, out)
        shaders = os.path.join(out, "shaders")
        if not args.force:
            check_version(shaders)
        edit_gbuffers_water(shaders)
        edit_settings(shaders)
        edit_properties(shaders)
        edit_lang(shaders)
        with open(os.path.join(out, "NORMAL-PATCH.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write(README_IN_PACK)
    except PatchError as e:
        shutil.rmtree(out, ignore_errors=True)   # leave nothing half-patched behind
        sys.exit("Patch failed: %s" % e)
    except Exception:
        shutil.rmtree(out, ignore_errors=True)
        raise

    print("Done. Patched pack written to:\n  " + out)
    print("Copy that folder into your .minecraft/shaderpacks/ and select it in the Iris shader menu.")
    print("Option: Shader options > Environment > Water > Normal Fix (DH).")


if __name__ == "__main__":
    main()
