import { describe, expect, it } from "vitest";
import { concat, encodeVarint, field, readFields, text, utf8 } from "../src/proto/wire.js";

const msg = (...parts: Uint8Array[]) => concat(...parts);
const utf8Payload = (s: string) => new TextEncoder().encode(s);

describe("readFields", () => {
  it("round-trips varint, length-delimited and fixed fields", () => {
    const buf = msg(field(1, 2, utf8Payload("devin-cli")), field(2, 0, 3000n), field(7, 0, 5n));
    const fields = readFields(buf);
    expect(fields.map((f) => f.field)).toEqual([1, 2, 7]);
    expect(text(fields[0])).toBe("devin-cli");
    expect(fields[1]?.varint).toBe(3000n);
  });

  it("keeps repeated fields in order", () => {
    const buf = msg(utf8(3, "a"), utf8(3, "b"), utf8(10, "c"));
    expect(
      readFields(buf)
        .filter((f) => f.field === 3)
        .map(text),
    ).toEqual(["a", "b"]);
  });

  it("throws on group wire types instead of guessing", () => {
    expect(() => readFields(Uint8Array.of(0x3b))).toThrow(); // field 7, wire 3 (SGROUP)
    expect(() => readFields(Uint8Array.of(0x0f))).toThrow(); // field 1, wire 7
  });

  it("throws on a truncated length-delimited field", () => {
    expect(() => readFields(Uint8Array.of(0x0a, 0x05, 0x61))).toThrow();
  });

  it("throws on field numbers above the protobuf maximum instead of re-encoding them wrong", () => {
    // Field 2^29, wire 2: legal varint, illegal field. Re-encoding it through Number would
    // silently change the tag, so it must fail at read time and fall back to passthrough.
    const tag = encodeVarint(BigInt(0x20000000 * 8 + 2));
    expect(() => readFields(concat(tag, Uint8Array.of(0)))).toThrow();
  });
});
