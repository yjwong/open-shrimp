namespace OpenShrimp.Tray.Core;

/// <summary>
/// Reads the one config value the tray needs before any core is running: the
/// instance name, which scopes the control endpoint and the scheduled task.
///
/// Deliberately not a YAML parser. Everything else the tray needs about the
/// config it gets from the core over the control channel, and everything it
/// writes goes through "openshrimp config write" — so the schema stays in one
/// language. This reads a single top-level scalar and nothing more.
/// </summary>
internal static class ConfigPeek
{
    public static string? ReadInstanceName(string configPath)
    {
        try
        {
            if (!File.Exists(configPath)) return null;

            foreach (var raw in File.ReadLines(configPath))
            {
                var line = raw.TrimEnd();
                // Top-level keys only: an indented instance_name belongs to
                // some nested mapping and is not the one we mean.
                if (line.Length == 0 || char.IsWhiteSpace(raw[0]) || line.StartsWith('#')) continue;
                if (!line.StartsWith("instance_name:", StringComparison.Ordinal)) continue;

                var value = line["instance_name:".Length..].Trim();
                var comment = value.IndexOf('#');
                if (comment >= 0) value = value[..comment].Trim();
                value = value.Trim('"', '\'');

                return string.IsNullOrEmpty(value) || value == "null" || value == "~" ? null : value;
            }
        }
        catch (IOException)
        {
            // An unreadable config is the default instance's problem to
            // report, not something to fail the tray over.
        }
        return null;
    }
}
