// Tiny launcher for the PoE Flipper stash window — starts it and exits.
// Path-relative, like launcher.cs: works from whatever folder it sits in
// alongside stash_window.py and the .venv.
using System;
using System.Diagnostics;
using System.IO;

static class Launcher
{
    static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        var psi = new ProcessStartInfo
        {
            FileName = Path.Combine(root, ".venv", "Scripts", "pythonw.exe"),
            Arguments = "\"" + Path.Combine(root, "stash_window.py") + "\"",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        Process.Start(psi);
    }
}
