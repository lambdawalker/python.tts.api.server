"""Optional official MCP SDK transport over the same application services."""

import json

import anyio
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .auth import caller
from .errors import DomainError
from .models import REQUESTS, VoiceRegistration


def object_schema(properties=None, required=None):
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def tool_definitions():
    model = {"model": {"type": "string", "default": "default"}}
    string = {"type": "string", "minLength": 1}
    definitions = {
        "list_models": ("Discover installed model profiles and readiness.", object_schema()),
        "get_capabilities": (
            "Discover effective controls before submitting work.",
            object_schema(model),
        ),
        "get_guidance": (
            "Read guidance; refresh when deployment, model or revision changes.",
            object_schema(dict(model, feature=string), ["feature"]),
        ),
        "list_voices": (
            "List compatible deployment-local voice IDs and aliases.",
            object_schema(model),
        ),
        "clone_voice": (
            "Register uploaded reference asset IDs as a voice; does not train or generate.",
            VoiceRegistration.model_json_schema(),
        ),
        "delete_voice": (
            "Remove a voice registration, retaining shared assets.",
            object_schema({"voice_id": string}, ["voice_id"]),
        ),
        "validate_speech": (
            "Validate caller-authored input without rewriting or inference.",
            REQUESTS["tts"].model_json_schema(),
        ),
        "get_job": (
            "Read independent job status and authorized result asset IDs.",
            object_schema({"job_id": string}, ["job_id"]),
        ),
        "cancel_job": (
            "Request cancellation; running inference may finish before it can stop.",
            object_schema({"job_id": string}, ["job_id"]),
        ),
    }
    for name, operation in [
        ("generate_speech", "tts"),
        ("design_voice", "voice_design"),
        ("convert_voice", "voice_conversion"),
    ]:
        schema = REQUESTS[operation].model_json_schema()
        schema["properties"]["idempotency_key"] = {"type": "string", "pattern": "^[!-~]{1,256}$"}
        definitions[name] = (
            "Submit a durable job and return promptly. Read capabilities and guidance "
            "first; use HTTP for uploads, downloads and SSE.",
            schema,
        )
    readonly = {
        "list_models",
        "get_capabilities",
        "get_guidance",
        "list_voices",
        "validate_speech",
        "get_job",
    }
    return [
        types.Tool(
            name=name,
            description=description,
            inputSchema=schema,
            annotations=types.ToolAnnotations(
                readOnlyHint=name in readonly,
                destructiveHint=name in {"delete_voice", "cancel_job"},
                openWorldHint=False,
            ),
        )
        for name, (description, schema) in definitions.items()
    ]


def dispatch(service, owner, name, arguments):
    schemas = {t.name: t.inputSchema for t in tool_definitions()}
    if name not in schemas:
        raise DomainError("unsupported_feature", "Unknown tool.")
    errors = list(Draft202012Validator(schemas[name]).iter_errors(arguments))
    if errors:
        raise DomainError("invalid_request", errors[0].message, ".".join(map(str, errors[0].path)))
    args = dict(arguments)
    if name == "list_models":
        return service.list_models()
    if name == "get_capabilities":
        return service.capabilities(args.get("model"))
    if name == "get_guidance":
        return service.guidance(args["feature"], args.get("model"))
    if name == "list_voices":
        return {"voices": service.list_voices(owner, args.get("model"))}
    if name == "clone_voice":
        return service.register_voice(owner, args)
    if name == "delete_voice":
        service.delete_voice(owner, args["voice_id"])
        return {"deleted": True}
    if name == "validate_speech":
        return service.validate_speech(owner, args)
    if name == "get_job":
        return service.get_job(owner, args["job_id"])
    if name == "cancel_job":
        return service.cancel_job(owner, args["job_id"])
    operation = {
        "generate_speech": "tts",
        "design_voice": "voice_design",
        "convert_voice": "voice_conversion",
    }[name]
    key = args.pop("idempotency_key", None)
    return service.submit(owner, operation, args, key)


def create_mcp(service):
    server = Server("tts-api-server")

    @server.list_tools()
    async def list_tools():
        return tool_definitions()

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            result = await anyio.to_thread.run_sync(
                dispatch, service, caller.get(), name, arguments
            )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(result))],
                structuredContent=result,
            )
        except DomainError as exc:
            result = exc.payload()
            return types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text=json.dumps(result))],
                structuredContent=result,
            )
        except Exception:
            result = DomainError(
                "internal_error", "The operation could not be completed."
            ).payload()
            return types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text=json.dumps(result))],
                structuredContent=result,
            )

    return StreamableHTTPSessionManager(server, stateless=True, json_response=True)
