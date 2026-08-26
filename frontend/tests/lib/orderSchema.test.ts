import { describe, expect, it } from "vitest";

import {
  encodeSingleFieldOrder,
  classifyStartOrderSchema,
  WORK_ITEM_ORDER_SCHEMA_REVISION,
  preValidateOrderValue,
  summarizeOrderSchema,
  typeLabel
} from "../../src/lib/orderSchema";

describe("summarizing an order's schema for a human", () => {
  it("lists every declared field with its type and whether it is required", () => {
    const document = {
      type: "object",
      required: ["message"],
      properties: {
        message: { type: "string", minLength: 1 },
        tone: { type: "string" }
      }
    };
    const summary = summarizeOrderSchema(document);
    expect(summary.fields).toEqual([
      { name: "message", types: ["string"], required: true },
      { name: "tone", types: ["string"], required: false }
    ]);
  });

  it("names the one field a human editor may fill as plain text", () => {
    const document = {
      type: "object",
      required: ["message"],
      properties: { message: { type: "string" } }
    };
    expect(summarizeOrderSchema(document).singleRequiredStringField).toBe("message");
  });

  it("names no human field when a declared type is not a bare object", () => {
    expect(
      summarizeOrderSchema({
        type: "array",
        required: ["message"],
        properties: { message: { type: "string" } }
      }).singleRequiredStringField
    ).toBeNull();
  });

  it("names no human field when more than one field is required", () => {
    const document = {
      type: "object",
      required: ["message", "prior_transcript", "dropped_oldest_messages"],
      properties: {
        message: { type: "string", minLength: 1 },
        prior_transcript: { type: "array" },
        dropped_oldest_messages: { type: "integer" }
      }
    };
    const summary = summarizeOrderSchema(document);
    expect(summary.singleRequiredStringField).toBeNull();
    expect(summary.fields).toHaveLength(3);
  });

  it("names no human field when the one required field is not a string", () => {
    const document = {
      type: "object",
      required: ["portions"],
      properties: { portions: { type: "integer" } }
    };
    expect(summarizeOrderSchema(document).singleRequiredStringField).toBeNull();
  });

  it("reads a schema declaring no properties or required fields as empty", () => {
    expect(summarizeOrderSchema(true)).toEqual({ fields: [], singleRequiredStringField: null });
    expect(summarizeOrderSchema({})).toEqual({ fields: [], singleRequiredStringField: null });
  });
});

describe("typeLabel", () => {
  it("joins a union of declared types and falls back to 'any'", () => {
    expect(typeLabel(["string"])).toBe("string");
    expect(typeLabel(["string", "null"])).toBe("string or null");
    expect(typeLabel(null)).toBe("any");
  });
});

describe("pre-validating typed JSON against an order's schema, shallowly", () => {
  const briefSchema = {
    type: "object",
    required: ["message", "prior_transcript", "dropped_oldest_messages"],
    additionalProperties: false,
    properties: {
      message: { type: "string", minLength: 1 },
      prior_transcript: { type: "array" },
      dropped_oldest_messages: { type: "integer", minimum: 0 }
    }
  };

  it("refuses text that is not valid JSON", () => {
    const verdict = preValidateOrderValue("{help}", briefSchema);
    expect(verdict).toEqual({ kind: "invalid", message: "This is not valid JSON.", fields: [] });
  });

  it("names every missing required field by its own pointer", () => {
    const verdict = preValidateOrderValue('{"message": "help"}', briefSchema);
    expect(verdict.kind).toBe("invalid");
    expect(verdict.kind === "invalid" && verdict.fields).toEqual([
      { path: "/prior_transcript", reason: "'prior_transcript' is a required property" },
      { path: "/dropped_oldest_messages", reason: "'dropped_oldest_messages' is a required property" }
    ]);
  });

  it("names a field whose value plainly disagrees with its declared type", () => {
    const verdict = preValidateOrderValue(
      '{"message": 7, "prior_transcript": [], "dropped_oldest_messages": 0}',
      briefSchema
    );
    expect(verdict.kind).toBe("invalid");
    expect(verdict.kind === "invalid" && verdict.fields).toEqual([
      { path: "/message", reason: "must be a string" }
    ]);
  });

  it("names a field a closed schema does not declare", () => {
    const verdict = preValidateOrderValue(
      '{"message": "help", "prior_transcript": [], "dropped_oldest_messages": 0, "extra": true}',
      briefSchema
    );
    expect(verdict.kind).toBe("invalid");
    expect(verdict.kind === "invalid" && verdict.fields).toEqual([
      {
        path: "/extra",
        reason: "'extra' is not declared, and a property this schema does not declare is refused"
      }
    ]);
  });

  it("accepts a value the schema admits, and leaves what it cannot see to the server", () => {
    expect(
      preValidateOrderValue(
        '{"message": "help", "prior_transcript": [], "dropped_oldest_messages": -1}',
        briefSchema
      )
    ).toEqual({ kind: "valid" });
  });

  it("checks only the top-level type against a schema declaring one", () => {
    expect(preValidateOrderValue("7", { type: "integer" })).toEqual({ kind: "valid" });
    expect(preValidateOrderValue('"seven"', { type: "integer" })).toEqual({
      kind: "invalid",
      message: "The value must be a JSON integer.",
      fields: []
    });
  });

  it("accepts anything against a schema this profile does not further constrain", () => {
    expect(preValidateOrderValue('{"anything":"goes"}', true)).toEqual({ kind: "valid" });
  });
});

describe("classifying a schema for the start sheet", () => {
  it("recognizes the canonical work-item document and ordinary object fields", () => {
    expect(
      classifyStartOrderSchema({
        title: "work item",
        type: "object",
        additionalProperties: false,
        required: ["body", "change_marker", "digest", "kind", "observed_at", "reference"],
        properties: {
          body: { type: "string" },
          change_marker: { type: "string" },
          digest: { type: "string" },
          kind: { type: "string" },
          observed_at: { type: "string" },
          reference: { type: "string" }
        }
      }, WORK_ITEM_ORDER_SCHEMA_REVISION)
    ).toEqual({ kind: "work_item" });
    expect(
      classifyStartOrderSchema({
        type: "object",
        properties: { portions: { type: "integer" } },
        required: ["portions"]
      }, "schema-portions")
    ).toEqual({ kind: "inline_object" });
  });

  it("refuses a noncanonical work-item lookalike before it could make a work-item start", () => {
    expect(
      classifyStartOrderSchema({
        title: "work item",
        type: "object",
        additionalProperties: false,
        required: ["body", "change_marker", "digest", "kind", "observed_at", "reference"],
        properties: {
          body: { type: "string" },
          change_marker: { type: "string" },
          digest: { type: "string" },
          kind: { type: "string" },
          observed_at: { type: "string" },
          reference: { type: "string" }
        }
      }, "f".repeat(64))
    ).toMatchObject({ kind: "unsupported" });
  });

  it.each([true, { type: "boolean" }, { type: "array" }, { type: "object", properties: { nested: { type: "object" } } }])(
    "refuses an unsupported start shape before it can be serialized",
    (document) => {
      expect(classifyStartOrderSchema(document, "schema-other").kind).toBe("unsupported");
    }
  );
});

describe("encodeSingleFieldOrder", () => {
  it("wraps trimmed typed text into the one field the schema names", () => {
    expect(encodeSingleFieldOrder("message", "  help me build this  ")).toBe(
      '{"message":"help me build this"}'
    );
  });

  it("escapes what JSON must escape", () => {
    expect(JSON.parse(encodeSingleFieldOrder("message", 'say "hi"'))).toEqual({
      message: 'say "hi"'
    });
  });
});
