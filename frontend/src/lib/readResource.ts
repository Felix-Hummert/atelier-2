export type ReadRequest<Failure> =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "failed"; failure: Failure };

export interface RetainedRead<Value, Failure> {
  confirmed: Value | null;
  generation: number;
  request: ReadRequest<Failure>;
}

export interface BegunRead<Value, Failure> {
  read: RetainedRead<Value, Failure>;
  generation: number;
}

export function retainedRead<Value, Failure>(): RetainedRead<Value, Failure> {
  return { confirmed: null, generation: 0, request: { state: "idle" } };
}

export function beginRead<Value, Failure>(
  read: RetainedRead<Value, Failure>
): BegunRead<Value, Failure> {
  const generation = read.generation + 1;
  return {
    read: { ...read, generation, request: { state: "loading" } },
    generation
  };
}

export function confirmRead<Value, Failure>(
  read: RetainedRead<Value, Failure>,
  generation: number,
  confirmed: Value
): RetainedRead<Value, Failure> {
  if (read.generation !== generation) return read;
  return { confirmed, generation, request: { state: "idle" } };
}

export function failRead<Value, Failure>(
  read: RetainedRead<Value, Failure>,
  generation: number,
  failure: NoInfer<Failure>
): RetainedRead<Value, Failure> {
  if (read.generation !== generation) return read;
  return { ...read, request: { state: "failed", failure } };
}

export function updateConfirmed<Value, Failure>(
  read: RetainedRead<Value, Failure>,
  confirmed: Value
): RetainedRead<Value, Failure> {
  return { ...read, confirmed };
}
