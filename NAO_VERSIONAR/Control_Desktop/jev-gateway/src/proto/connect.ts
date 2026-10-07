/** Connect protocol framing: `[flags:1][length:4 BE][payload]` per message; the stream's last
 *  frame carries flag 0x2 and a JSON trailer. Compression (flag 0x1) is left to the caller —
 *  the adapter passes such requests through untouched. */
import { concat } from "./wire.js";

export interface Frame {
  flags: number;
  payload: Uint8Array;
}

export function peel(buf: Uint8Array): Frame[] {
  const frames: Frame[] = [];
  let off = 0;
  while (off < buf.length) {
    if (off + 5 > buf.length) throw new Error("truncated envelope header");
    const flags = buf[off]!;
    const len = new DataView(buf.buffer, buf.byteOffset + off + 1, 4).getUint32(0);
    if (off + 5 + len > buf.length) throw new Error("truncated envelope payload");
    frames.push({ flags, payload: buf.subarray(off + 5, off + 5 + len) });
    off += 5 + len;
  }
  return frames;
}

export function frame(payload: Uint8Array, flags = 0): Uint8Array<ArrayBuffer> {
  const header = new Uint8Array(5);
  header[0] = flags;
  new DataView(header.buffer).setUint32(1, payload.length);
  return concat(header, payload);
}
