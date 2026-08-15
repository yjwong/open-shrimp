using System.Text.Json;
using System.Text.Json.Serialization;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// Wire types for the control channel. Mirrors src/open_shrimp/control/protocol.py —
/// newline-delimited JSON, requests carry an id, events carry a name and no id.
/// </summary>
internal sealed class ControlResponse
{
    [JsonPropertyName("id")] public JsonElement? Id { get; set; }
    [JsonPropertyName("result")] public JsonElement? Result { get; set; }
    [JsonPropertyName("error")] public ControlError? Error { get; set; }
    [JsonPropertyName("event")] public string? Event { get; set; }
    [JsonPropertyName("data")] public JsonElement? Data { get; set; }

    [JsonIgnore] public bool IsEvent => Event is not null;
}

internal sealed class ControlError
{
    [JsonPropertyName("code")] public string Code { get; set; } = "";
    [JsonPropertyName("message")] public string Message { get; set; } = "";
}

internal sealed class CoreStatus
{
    [JsonPropertyName("protocol")] public int Protocol { get; set; }
    [JsonPropertyName("version")] public string? Version { get; set; }
    [JsonPropertyName("pid")] public int Pid { get; set; }
    [JsonPropertyName("state")] public string State { get; set; } = "unknown";
    [JsonPropertyName("config_path")] public string? ConfigPath { get; set; }
    [JsonPropertyName("instance_name")] public string? InstanceName { get; set; }
    [JsonPropertyName("contexts")] public List<string> Contexts { get; set; } = new();
    [JsonPropertyName("bot_username")] public string? BotUsername { get; set; }
    [JsonPropertyName("error")] public string? Error { get; set; }
}

internal static class ControlJson
{
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };
}
