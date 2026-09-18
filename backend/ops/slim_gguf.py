"""
Shrink a GGUF for a text-only workload without touching the text weights.

Two findings on gemma4:e4b, both from reading the file rather than guessing:

  1. It is published as "Q4_K_M", but `per_layer_token_embd.weight` — a
     [10752 x 262144] lookup table — is left at BF16: 5,376 MiB, 65% of the whole
     text tower. It is read on every token, so on an 8 GB box it cannot stay in
     page cache and the weights are re-read from disk token after token
     (llama-server: 5.7 GB resident of 9.6 GB, 11,255 major faults).
  2. 1,411 of its 2,131 tensors are the vision (16 blocks) and audio (12 blocks)
     towers, ~990 MiB that a text-only assistant never reads.

llama-quantize cannot do either job on its own: COPY mode ignores --tensor-type,
and any real base type would requantize the other 719 tensors as well. So this
rewrites the one tensor and drops the towers, copying every other byte through.

Quantizing a lookup table is the mildest quantization there is — a row is read,
not accumulated through a matmul — which is why Q8_0 is the default here.

  python slim_gguf.py in.gguf out.gguf [--quant q8_0|q4_0] [--keep-multimodal]
"""
import struct
import sys

import numpy as np

QK = 32
GGML_BF16, GGML_Q8_0, GGML_Q4_0 = 30, 8, 2      # ggml type ids, not the llama_ftype enum
TARGET = "per_layer_token_embd.weight"
DROP_TENSOR_PREFIXES = ("v.", "a.", "mm.")
DROP_KV_PREFIXES = ("gemma4.vision.", "gemma4.audio.", "clip.")
BLOCK_BYTES = {GGML_Q8_0: 34, GGML_Q4_0: 18}
ROWS_PER_CHUNK = 2048
TYPE_SIZES = {0: (1, 4), 1: (1, 2), 2: (32, 18), 3: (32, 20), 6: (32, 22), 7: (32, 24),
              8: (32, 34), 10: (256, 84), 11: (256, 110), 12: (256, 144), 13: (256, 176),
              14: (256, 210), 15: (256, 292), 24: (1, 1), 25: (1, 2), 26: (1, 4), 30: (1, 2)}
SCALAR = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def parse(f):
    """Header walk. Returns (version, kv entries as byte ranges, tensors, data_start, alignment)."""
    assert f.read(4) == b"GGUF", "not a GGUF file"
    version = struct.unpack("<I", f.read(4))[0]
    n_tensors, n_kv = struct.unpack("<QQ", f.read(16))

    def rs():
        n = struct.unpack("<Q", f.read(8))[0]
        return f.read(n)

    def skip(t):
        if t == 8:
            rs()
        elif t == 9:
            et = struct.unpack("<I", f.read(4))[0]
            for _ in range(struct.unpack("<Q", f.read(8))[0]):
                skip(et)
        else:
            f.read(SCALAR[t])

    kv, alignment = [], 32
    for _ in range(n_kv):
        start = f.tell()
        key = rs().decode()
        t = struct.unpack("<I", f.read(4))[0]
        value_at = f.tell()
        skip(t)
        if key == "general.alignment":
            f.seek(value_at)
            alignment = struct.unpack("<I", f.read(4))[0]
            f.seek(value_at)
            skip(t)
        kv.append({"key": key, "start": start, "end": f.tell()})

    tensors = []
    for _ in range(n_tensors):
        name = rs().decode()
        dims = [struct.unpack("<Q", f.read(8))[0]
                for _ in range(struct.unpack("<I", f.read(4))[0])]
        ttype, off = struct.unpack("<IQ", f.read(12))
        tensors.append({"name": name, "dims": dims, "type": ttype, "off": off})
    data_start = (f.tell() + alignment - 1) // alignment * alignment
    return version, kv, tensors, data_start, alignment


def nbytes(t):
    ne = 1
    for d in t["dims"]:
        ne *= d
    block, size = TYPE_SIZES[t["type"]]
    return ne // block * size


def quantize_rows(vals, quant):
    """vals: (n_blocks, 32) float32 -> packed block bytes."""
    if quant == GGML_Q8_0:
        d = (np.abs(vals).max(axis=1) / 127.0).astype(np.float32)
        inv = np.where(d > 0, 1.0 / np.where(d > 0, d, 1.0), 0.0)
        out = np.empty((vals.shape[0], 34), dtype=np.uint8)
        out[:, :2] = d.astype(np.float16).view(np.uint8).reshape(-1, 2)
        out[:, 2:] = np.rint(vals * inv[:, None]).clip(-128, 127).astype(np.int8).view(np.uint8)
        return out
    # Q4_0: one fp16 scale, then 16 bytes holding 32 nibbles (low half first, then high half)
    idx = np.abs(vals).argmax(axis=1)
    amax = vals[np.arange(vals.shape[0]), idx]
    d = (amax / -8.0).astype(np.float32)
    inv = np.where(d != 0, 1.0 / np.where(d != 0, d, 1.0), 0.0)
    q = np.rint(vals * inv[:, None] + 8.5).clip(0, 15).astype(np.uint8)
    out = np.empty((vals.shape[0], 18), dtype=np.uint8)
    out[:, :2] = d.astype(np.float16).view(np.uint8).reshape(-1, 2)
    out[:, 2:] = q[:, :16] | (q[:, 16:] << 4)
    return out


def main():
    src_path, dst_path = sys.argv[1], sys.argv[2]
    quant = GGML_Q4_0 if "--quant" in sys.argv and sys.argv[sys.argv.index("--quant") + 1] == "q4_0" else GGML_Q8_0
    drop_mm = "--keep-multimodal" not in sys.argv

    src = open(src_path, "rb")
    version, kv, tensors, data_start, alignment = parse(src)

    keep_kv = [e for e in kv if not (drop_mm and e["key"].startswith(DROP_KV_PREFIXES))]
    keep = [t for t in tensors if not (drop_mm and t["name"].startswith(DROP_TENSOR_PREFIXES))]
    target = next(t for t in keep if t["name"] == TARGET)
    assert target["type"] == GGML_BF16, f"{TARGET} is type {target['type']}, expected BF16"
    row_len, n_rows = target["dims"][0], target["dims"][1]
    assert row_len % QK == 0
    target_bytes = n_rows * (row_len // QK) * BLOCK_BYTES[quant]

    dropped = sum(nbytes(t) for t in tensors if t not in keep)
    print(f"tensors {len(tensors)} -> {len(keep)}  (dropped {dropped / 2**20:.0f} MiB of vision/audio)")
    print(f"{TARGET}: {nbytes(target) / 2**20:.0f} MiB BF16 -> {target_bytes / 2**20:.0f} MiB "
          f"{'Q8_0' if quant == GGML_Q8_0 else 'Q4_0'}")

    for t in sorted(keep, key=lambda t: t["off"]):
        t["new_size"] = target_bytes if t is target else nbytes(t)
    cursor = 0
    for t in sorted(keep, key=lambda t: t["off"]):
        t["new_off"] = cursor
        cursor += (t["new_size"] + alignment - 1) // alignment * alignment

    out = open(dst_path, "wb")
    out.write(b"GGUF" + struct.pack("<I", version) + struct.pack("<QQ", len(keep), len(keep_kv)))
    for e in keep_kv:
        src.seek(e["start"])
        out.write(src.read(e["end"] - e["start"]))
    for t in keep:
        name = t["name"].encode()
        out.write(struct.pack("<Q", len(name)) + name + struct.pack("<I", len(t["dims"])))
        for d in t["dims"]:
            out.write(struct.pack("<Q", d))
        out.write(struct.pack("<IQ", quant if t is target else t["type"], t["new_off"]))
    while out.tell() % alignment:
        out.write(b"\0")
    body = out.tell()

    for t in sorted(keep, key=lambda t: t["new_off"]):
        out.seek(body + t["new_off"])
        src.seek(data_start + t["off"])
        if t is target:
            done = 0
            while done < n_rows:
                rows = min(ROWS_PER_CHUNK, n_rows - done)
                raw = np.frombuffer(src.read(rows * row_len * 2), dtype=np.uint16)
                vals = (raw.astype(np.uint32) << 16).view(np.float32).reshape(-1, QK)
                out.write(quantize_rows(vals, quant).tobytes())
                done += rows
        else:
            left = t["new_size"]
            while left:
                chunk = src.read(min(left, 1 << 24))
                out.write(chunk)
                left -= len(chunk)
    out.seek(body + cursor)
    out.truncate()
    out.close()
    print(f"wrote {dst_path}")


if __name__ == "__main__":
    main()
