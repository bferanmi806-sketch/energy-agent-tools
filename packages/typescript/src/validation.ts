import { Ajv } from "ajv";
import formatsPlugin from "ajv-formats";
import { EnergyProtocolError } from "./transport.js";

const validator = new Ajv({ strict: false, allErrors: false });
const addFormats = typeof formatsPlugin === "function" ? formatsPlugin : formatsPlugin.default;
addFormats(validator);

/** Compile the same generated schema used to derive the caller's type. */
export function parser<T>(schema: object | boolean): (value: unknown) => T {
  const validate = validator.compile<T>(schema);
  return (value: unknown): T => {
    if (!validate(value)) {
      throw new EnergyProtocolError("Gateway payload does not match its contract.");
    }
    return value;
  };
}
