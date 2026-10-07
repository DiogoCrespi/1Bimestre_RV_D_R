/** Protobuf wire format, read and written without a schema. Only what the exa adapter needs:
 *  varint (0), fixed64 (1), length-delimited (2), fixed32 (5). Anything else — groups, the
 *  illegal wires — throws, and the caller fails open to passthrough. */

export interface WireField {
  field: number;
  wire: 0 | 1 | 2 | 5;
  varint?: bigint;
  bytes?: Uint8Array;
}

export function encodeVarint(value: number | bigint): Uint8Array {
  let v = BigInt(value);
  const out: number[] = [];
  do {
    let b = Number(v & 0x7fn);
    v >>= 7n;
    if (v > 0n) b |= 0x80;
    out.push(b);
  } while (v > 0n);
  return Uint8Array.from(out);
}

export function readVarint(buf: Uint8Array, off: number): [bigint, number] {
  let result = 0n;
  let shift = 0n;
  let pos = off;
  while (pos < buf.length) {
    const b = buf[pos++]!;
    result |= BigInt(b & 0x7f) << shift;
    if (!(b & 0x80)) return [result, pos];
    shift += 7n;
    if (shift > 70n) break;
  }
  throw new Error("truncated varint");
}

export function readFields(buf: Uint8Array): WireField[] {
  const out: WireField[] = [];
  let off = 0;
  while (off < buf.length) {
    const [tag, afterTag] = readVarint(buf, off);
    off = afterTag;
    const field = Number(tag >> 3n);
    const wire = Number(tag & 7n);
    if (field === 0) throw new Error("field 0 is not legal");
    // Protobuf caps field numbers at 2^29-1; past that, Number() precision would let a tag
    // re-encode differently than it arrived, which is worse than refusing to touch it.
    if (field > 0x1fffffff) throw new Error(`field ${field} is out of range`);
    if (wire === 0) {
      const [varint, next] = readVarint(buf, off);
      off = next;
      out.push({ field, wire, varint });
    } else if (wire === 2) {
      const [len, next] = readVarint(buf, off);
      const end = next + Number(len);
      if (end > buf.length) throw new Error("truncated field");
      out.push({ field, wire, bytes: buf.subarray(next, end) });
      off = end;
    } else if (wire === 5) {
      if (off + 4 > buf.length) throw new Error("truncated fixed32");
      out.push({ field, wire, bytes: buf.subarray(off, off + 4) });
      off += 4;
    } else if (wire === 1) {
      if (off + 8 > buf.length) throw new Error("truncated fixed64");
      out.push({ field, wire, bytes: buf.subarray(off, off + 8) });
      off += 8;
    } else {
      throw new Error(`unsupported wire type ${wire} on field ${field}`);
    }
  }
  return out;
}

/** Encode one field. `wire` is the wire type the field number travels with. */
export function field(fieldNo: number, wire: 0 | 2, payload: bigint | Uint8Array): Uint8Array {
  const tag = encodeVarint(BigInt(fieldNo * 8 + wire));
  if (wire === 0) return concat(tag, encodeVarint(payload as bigint));
  const data = payload as Uint8Array;
  return concat(tag, encodeVarint(data.length), data);
}

export const utf8 = (fieldNo: number, value: string): Uint8Array =>
  field(fieldNo, 2, new TextEncoder().encode(value));

export function concat(...parts: Uint8Array[]): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let off = 0;
  for (const part of parts) {
    out.set(part, off);
    off += part.length;
  }
  return out;
}

/** Lossy UTF-8 for a length-delimited field; other shapes and absence are `""`. */
export function text(field: WireField | undefined): string {
  if (!field?.bytes) return "";
  return new TextDecoder("utf-8", { fatal: false }).decode(field.bytes);
}
