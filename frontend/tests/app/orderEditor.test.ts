import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "svelte";

import OrderEditor from "../../src/components/OrderEditor.svelte";
import type { JsonSchemaDocument } from "../../src/api/client";
import type { OrderSchemaReadFailure, OrderSchemaResource } from "../../src/lib/orderSchema";
import { summarizeOrderSchema } from "../../src/lib/orderSchema";
import { confirmRead, retainedRead, type RetainedRead } from "../../src/lib/readResource";

type OrderEditorProps = ComponentProps<typeof OrderEditor>;

const order = { name: "brief", schema: { ref: "conductor-brief", revision: "hash1" } };

function confirmedRead(
  document: JsonSchemaDocument
): RetainedRead<OrderSchemaResource, OrderSchemaReadFailure> {
  const started = retainedRead<OrderSchemaResource, OrderSchemaReadFailure>();
  return confirmRead(started, started.generation, {
    summary: summarizeOrderSchema(document),
    document
  });
}

function baseProps(overrides: Partial<OrderEditorProps> = {}): OrderEditorProps {
  return {
    order,
    value: "",
    error: null,
    fieldErrors: [],
    schemaRead: retainedRead<OrderSchemaResource, OrderSchemaReadFailure>(),
    busy: false,
    starting: false,
    onInput: vi.fn(),
    onRetrySchema: vi.fn(),
    ...overrides
  };
}

afterEach(cleanup);

describe("the schema an order editor shows", () => {
  it("lists every declared field, its type and whether it is required", () => {
    render(OrderEditor, {
      props: baseProps({
        schemaRead: confirmedRead({
          type: "object",
          required: ["message"],
          properties: { message: { type: "string" }, tone: { type: "string" } }
        })
      })
    });

    const fields = screen.getByRole("region", { name: "Fields of brief" });
    expect(fields.textContent).toContain("message");
    expect(fields.textContent).toContain("string");
    expect(fields.textContent).toContain("Required");
    expect(fields.textContent).toContain("tone");
  });

  it("names a schema this closed profile does not further constrain, honestly", () => {
    render(OrderEditor, { props: baseProps({ schemaRead: confirmedRead(true) }) });
    expect(screen.getByText(/names no fields/)).toBeTruthy();
  });
});

describe("the human editor for a single required string field", () => {
  it("offers plain text, not JSON, and wraps what was typed into the schema's own field", async () => {
    const onInput = vi.fn();
    render(OrderEditor, {
      props: baseProps({
        schemaRead: confirmedRead({
          type: "object",
          required: ["message"],
          properties: { message: { type: "string" } }
        }),
        onInput
      })
    });

    const material = screen.getByLabelText("Material brief");
    expect(material.tagName).toBe("INPUT");
    await fireEvent.input(material, { target: { value: "help me finish this" } });
    expect(onInput).toHaveBeenCalledWith("help me finish this");
  });

  it("falls back to a JSON editor and an honest note when the schema needs more than one field", () => {
    render(OrderEditor, {
      props: baseProps({
        schemaRead: confirmedRead({
          type: "object",
          required: ["message", "prior_transcript", "dropped_oldest_messages"],
          properties: {
            message: { type: "string" },
            prior_transcript: { type: "array" },
            dropped_oldest_messages: { type: "integer" }
          }
        })
      })
    });

    const material = screen.getByLabelText("Material brief");
    expect(material.tagName).toBe("TEXTAREA");
    expect(screen.getByText(/more than one field/)).toBeTruthy();
  });
});

describe("refusal rendering on an order", () => {
  it("shows each field pointer beside its reason, and the top-level message once", () => {
    render(OrderEditor, {
      props: baseProps({
        schemaRead: confirmedRead({
          type: "object",
          required: ["message"],
          properties: { message: { type: "string" } }
        }),
        error: "This value does not match the schema above.",
        fieldErrors: [{ path: "/message", reason: "'message' is a required property" }]
      })
    });

    const alerts = screen.getAllByRole("alert").map((node) => node.textContent);
    expect(alerts.some((text) => text?.includes("This value does not match the schema above."))).toBe(
      true
    );
    expect(alerts.some((text) => text?.includes("/message") && text.includes("is a required property"))).toBe(
      true
    );
  });

  it("still shows the material field's own label while a schema fetch is retried", async () => {
    const onRetrySchema = vi.fn();
    render(OrderEditor, {
      props: baseProps({
        schemaRead: {
          confirmed: null,
          generation: 1,
          request: { state: "failed", failure: { kind: "unavailable", title: "Order schema unavailable" } }
        },
        onRetrySchema
      })
    });

    expect(screen.getByLabelText("Material brief")).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Retry schema for brief" }));
    expect(onRetrySchema).toHaveBeenCalledTimes(1);
  });
});
