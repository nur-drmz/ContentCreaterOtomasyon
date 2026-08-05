using System;
using System.Diagnostics;
using System.IO;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

internal static class StopCreatorConsole
{
    [STAThread]
    private static int Main(string[] args)
    {
        bool quiet = HasArgument(args, "--quiet");
        bool checkOnly = HasArgument(args, "--check");
        string projectRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(
            Path.DirectorySeparatorChar,
            Path.AltDirectorySeparatorChar);
        string consoleDir = Path.Combine(projectRoot, "creator_console");
        string requestedStorageRoot = ArgumentValue(args, "--storage-root");
        string dataDir = string.IsNullOrWhiteSpace(requestedStorageRoot)
            ? Path.Combine(projectRoot, ".console_data")
            : Path.GetFullPath(Path.IsPathRooted(requestedStorageRoot)
                ? requestedStorageRoot
                : Path.Combine(projectRoot, requestedStorageRoot));
        string controlPath = Path.Combine(dataDir, ".creator_console_control.json");
        string stopPath = Path.Combine(dataDir, ".creator_console_stop");

        if (!Directory.Exists(consoleDir))
        {
            Show(quiet, "creator_console klasoru bulunamadi.", true);
            return 2;
        }
        if (checkOnly)
        {
            return 0;
        }
        if (!File.Exists(controlPath))
        {
            Show(quiet, "Creator Console zaten kapali veya bu klasorden baslatilmamis.", false);
            return 0;
        }

        try
        {
            string control = File.ReadAllText(controlPath);
            Match pidMatch = Regex.Match(control, "\\\"pid\\\"\\s*:\\s*(\\d+)");
            Match tokenMatch = Regex.Match(
                control,
                "\\\"shutdown_token\\\"\\s*:\\s*\\\"([A-Za-z0-9_-]{20,})\\\"");
            if (!pidMatch.Success || !tokenMatch.Success)
            {
                Show(quiet, "Console kontrol dosyasi gecersiz; guvenli kapatma yapilmadi.", true);
                return 3;
            }

            int pid = int.Parse(pidMatch.Groups[1].Value);
            string token = tokenMatch.Groups[1].Value;
            Process process;
            try
            {
                process = Process.GetProcessById(pid);
            }
            catch (ArgumentException)
            {
                SafeDelete(controlPath);
                SafeDelete(stopPath);
                Show(quiet, "Creator Console zaten kapaliydi; eski kontrol kaydi temizlendi.", false);
                return 0;
            }

            string temporaryStop = stopPath + ".tmp." + Process.GetCurrentProcess().Id;
            File.WriteAllText(temporaryStop, token);
            if (File.Exists(stopPath))
            {
                File.Delete(stopPath);
            }
            File.Move(temporaryStop, stopPath);

            for (int attempt = 0; attempt < 100; attempt++)
            {
                if (process.HasExited || !File.Exists(controlPath))
                {
                    Show(quiet, "Creator Console kapatildi.", false);
                    return 0;
                }
                Thread.Sleep(100);
            }

            Show(quiet, "Console 10 saniye icinde kapanmadi. Sunucu terminalini kontrol edin.", true);
            return 4;
        }
        catch (Exception exc)
        {
            Show(quiet, "Creator Console kapatilirken hata olustu:\n" + exc.Message, true);
            return 1;
        }
    }

    private static bool HasArgument(string[] args, string expected)
    {
        foreach (string arg in args)
        {
            if (string.Equals(arg, expected, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }
        return false;
    }

    private static string ArgumentValue(string[] args, string expected)
    {
        for (int index = 0; index < args.Length; index++)
        {
            if (string.Equals(args[index], expected, StringComparison.OrdinalIgnoreCase)
                && index + 1 < args.Length)
            {
                return args[index + 1];
            }
            string prefix = expected + "=";
            if (args[index].StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            {
                return args[index].Substring(prefix.Length);
            }
        }
        return null;
    }

    private static void SafeDelete(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
        catch
        {
            // A stale control file is harmless; never kill a process as fallback.
        }
    }

    private static void Show(bool quiet, string message, bool error)
    {
        if (!quiet)
        {
            MessageBox.Show(
                message,
                error ? "Creator Console kapatilamadi" : "Creator Console",
                MessageBoxButtons.OK,
                error ? MessageBoxIcon.Error : MessageBoxIcon.Information);
        }
    }
}
