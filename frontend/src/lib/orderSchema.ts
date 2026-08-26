import type { InvalidField, JsonSchemaDocument } from "../api/client";

/** One field an order's schema names, read only far enough to summarize it. */
export interface OrderSchemaField {
  readonly name: string;
  readonly types: readonly string[] | null;
  readonly required: boolean;
}

export interface OrderSchemaSummary {
  readonly fields: readonly OrderSchemaField[];
  /**
   * The one field a human editor may ask for as plain text: present only when
   * the schema requires exactly one field, and that field is declared as a
   * bare string. Every other shape -- more than one required field, no
   * declared type, a non-string type -- keeps the JSON editor, because
   * wrapping the typed text into an object would either lose the other
   * required fields or guess a type this module was not told.
   */
  readonly singleRequiredStringField: string | null;
}

/** A schema document, read once, paired with the human summary read from it. */
export interface OrderSchemaResource {
  readonly summary: OrderSchemaSummary;
  readonly document: JsonSchemaDocument;
}

/** The start sheet's one honest rendering for a published V3 order schema. */
export type StartOrderSchemaShape =
  | { readonly kind: "work_item" }
  | { readonly kind: "inline_object" }
  | { readonly kind: "unsupported"; readonly reason: string };

export interface OrderSchemaReadFailure {
  readonly kind: "unavailable";
  readonly title: string;
}

export type OrderValueVerdict =
  | { readonly kind: "valid" }
  | {
      readonly kind: "invalid";
      readonly message: string;
      readonly fields: readonly InvalidField[];
  };

/**
 * The backend only accepts an observed work item beneath this published
 * revision. The workflow wire already carries that pin, so the start sheet
 * mirrors the server's discriminator instead of inferring it from field names.
 */
export const WORK_ITEM_ORDER_SCHEMA_REVISION =
  "e57e281851b809afc32527cdde2a2a76b033f4b6b4301ad592472147bc7c978a";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The `type` keyword's own declared names, single or a union -- never guessed. */
function declaredTypes(subschema: unknown): readonly string[] | null {
  if (!isRecord(subschema)) return null;
  const declared = subschema.type;
  if (typeof declared === "string") return [declared];
  if (Array.isArray(declared) && declared.every((entry) => typeof entry === "string")) {
    return declared as string[];
  }
  return null;
}

function declaredProperties(document: Record<string, unknown>): Record<string, unknown> {
  return isRecord(document.properties) ? document.properties : {};
}

function declaredRequired(document: Record<string, unknown>): readonly string[] {
  return Array.isArray(document.required)
    ? document.required.filter((entry): entry is string => typeof entry === "string")
    : [];
}

const WORK_ITEM_FIELDS = [
  "body",
  "change_marker",
  "digest",
  "kind",
  "observed_at",
  "reference"
] as const;

/**
 * The tracker order's published schema has one closed, neutral shape. The
 * browser checks the canonical shape as a second guard after its revision pin.
 */
function isWorkItemOrderSchema(document: Record<string, unknown>): boolean {
  const properties = declaredProperties(document);
  const required = declaredRequired(document);
  return (
    document.title === "work item" &&
    fieldTypesAreExactly(declaredTypes(document), "object") &&
    document.additionalProperties === false &&
    required.length === WORK_ITEM_FIELDS.length &&
    WORK_ITEM_FIELDS.every((field) => required.includes(field)) &&
    Object.keys(properties).length === WORK_ITEM_FIELDS.length &&
    WORK_ITEM_FIELDS.every((field) => field in properties)
  );
}

/**
 * A start never manufactures an object for a scalar or array schema. It can
 * render the canonical tracker picker and ordinary object fields; anything
 * else stays visibly unavailable until its own renderer exists.
 */
export function classifyStartOrderSchema(
  document: JsonSchemaDocument,
  schemaRevision: string
): StartOrderSchemaShape {
  if (!isRecord(document)) {
    return { kind: "unsupported", reason: "This order shape is not supported by the start sheet." };
  }
  if (isWorkItemOrderSchema(document)) {
    if (schemaRevision === WORK_ITEM_ORDER_SCHEMA_REVISION) return { kind: "work_item" };
    return {
      kind: "unsupported",
      reason: "This work-item-shaped order does not use the canonical work-item schema."
    };
  }
  if (!fieldTypesAreExactly(declaredTypes(document), "object")) {
    return { kind: "unsupported", reason: "This order must be an object to start here." };
  }
  const properties = declaredProperties(document);
  const required = declaredRequired(document);
  const fieldNames = [...new Set([...Object.keys(properties), ...required])];
  const supported = new Set(["boolean", "number", "integer", "string"]);
  if (
    fieldNames.some((name) => {
      const types = declaredTypes(properties[name]);
      return types === null || types.length !== 1 || !supported.has(types[0] ?? "");
    })
  ) {
    return { kind: "unsupported", reason: "This order has a field the start sheet cannot encode." };
  }
  return { kind: "inline_object" };
}

/**
 * Every field a schema document names, for a person who cannot read JSON
 * Schema. This reads only `type`, `properties` and `required` at the
 * document's own top level -- the same three facts the door already shows in
 * `atelier2.contracts.schemas_v3`'s vocabulary, never the full Draft 2020-12
 * evaluation that module alone owns.
 */
export function summarizeOrderSchema(document: JsonSchemaDocument): OrderSchemaSummary {
  if (!isRecord(document)) return { fields: [], singleRequiredStringField: null };
  const properties = declaredProperties(document);
  const required = declaredRequired(document);
  const names = [...new Set([...Object.keys(properties), ...required])];
  const fields = names.map((name) => ({
    name,
    types: declaredTypes(properties[name]),
    required: required.includes(name)
  }));
  const topLevelTypes = declaredTypes(document);
  const [onlyRequired] = required;
  const singleRequiredStringField =
    required.length === 1 &&
    onlyRequired !== undefined &&
    (topLevelTypes === null || fieldTypesAreExactly(topLevelTypes, "object")) &&
    fieldTypesAreExactly(declaredTypes(properties[onlyRequired]), "string")
      ? onlyRequired
      : null;
  return { fields, singleRequiredStringField };
}

function fieldTypesAreExactly(types: readonly string[] | null, only: string): boolean {
  return types !== null && types.length === 1 && types[0] === only;
}

/** The words a summarized field's declared type reads as, or "any" when none is named. */
export function typeLabel(types: readonly string[] | null): string {
  return types === null || types.length === 0 ? "any" : types.join(" or ");
}

type JsonValueKind = "null" | "boolean" | "integer" | "number" | "string" | "array" | "object";

function kindOf(value: unknown): JsonValueKind {
  if (value === null) return "null";
  if (typeof value === "boolean") return "boolean";
  if (Array.isArray(value)) return "array";
  if (typeof value === "string") return "string";
  if (typeof value === "number") return Number.isInteger(value) ? "integer" : "number";
  return "object";
}

function matchesOneDeclaredType(value: unknown, types: readonly string[]): boolean {
  const kind = kindOf(value);
  return types.some((candidate) => candidate === kind || (candidate === "number" && kind === "integer"));
}

/**
 * Whether typed JSON text plainly fails the schema above it, judged shallowly
 * and only by the vocabulary this file already reads: the top-level type, the
 * required properties, `additionalProperties: false`, and one property level
 * of declared types. This is advisory, never authoritative -- a shape this
 * check cannot see (`$ref`, `oneOf`, nested object or array schemas, `enum`,
 * string or number bounds) is left to the server's own evaluation
 * (`atelier2.contracts.schemas_v3`), which every start still asks. The point
 * is catching the round trip a person would otherwise only learn about from a
 * refused request, not replacing that evaluator.
 */
export function preValidateOrderValue(typed: string, document: JsonSchemaDocument): OrderValueVerdict {
  let parsed: unknown;
  try {
    parsed = JSON.parse(typed);
  } catch {
    return { kind: "invalid", message: "This is not valid JSON.", fields: [] };
  }
  if (!isRecord(document)) return { kind: "valid" };
  const topLevelTypes = declaredTypes(document);
  if (topLevelTypes !== null && !matchesOneDeclaredType(parsed, topLevelTypes)) {
    return {
      kind: "invalid",
      message: `The value must be a JSON ${typeLabel(topLevelTypes)}.`,
      fields: []
    };
  }
  if (!isRecord(parsed)) return { kind: "valid" };
  const properties = declaredProperties(document);
  const required = declaredRequired(document);
  const fields: InvalidField[] = [];
  for (const name of required) {
    if (!(name in parsed)) {
      fields.push({ path: `/${name}`, reason: `'${name}' is a required property` });
    }
  }
  if (document.additionalProperties === false) {
    for (const key of Object.keys(parsed)) {
      if (!(key in properties)) {
        fields.push({
          path: `/${key}`,
          reason: `'${key}' is not declared, and a property this schema does not declare is refused`
        });
      }
    }
  }
  for (const [name, value] of Object.entries(parsed)) {
    if (!(name in properties)) continue;
    const declared = declaredTypes(properties[name]);
    if (declared !== null && !matchesOneDeclaredType(value, declared)) {
      fields.push({ path: `/${name}`, reason: `must be a ${typeLabel(declared)}` });
    }
  }
  if (fields.length === 0) return { kind: "valid" };
  return { kind: "invalid", message: "This value does not match the schema above.", fields };
}

/**
 * What a person's plain text becomes for an order whose schema names exactly
 * one required string field: the single value a human editor asks for,
 * wrapped into the JSON object the schema actually requires. This draws the
 * same human/expert boundary `encodeWaitAnswer` (`./waitAnswer.ts`) draws for
 * a wait answer, moved from a bare string onto an order's declared shape.
 */
export function encodeSingleFieldOrder(field: string, typed: string): string {
  return JSON.stringify({ [field]: typed.trim() });
}
