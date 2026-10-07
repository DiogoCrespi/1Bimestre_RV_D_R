import { describe, expect, it } from "vitest";
import { frame, peel } from "../src/proto/connect.js";
import { concat } from "../src/proto/wire.js";

const u8 = (s: string) => new TextEncoder().encode(s);
const concatBytes = (...parts: Uint8Array[]) => concat(...parts);

describe("Connect envelopes", () => {
  it("peels one frame into flags and payload", () => {
    const body = frame(u8("abc"), 0);
    expect(peel(body)).toEqual([{ flags: 0, payload: u8("abc") }]);
  });

  it("peels a stream ending in the JSON trailer", () => {
    const body = concatBytes(frame(u8("x"), 0), frame(u8("{}"), 0x2));
    const frames = peel(body);
    expect(frames.map((f) => f.flags)).toEqual([0, 2]);
  });

  it("throws on a truncated frame instead of guessing", () => {
    const body = frame(u8("abcdef"), 0).subarray(0, 7); // header says 6, 2 present
    expect(() => peel(body)).toThrow();
  });
});
