using System;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Threading;
using System.Windows.Forms;

internal static class StartCreatorConsole
{
    private const string Host = "127.0.0.1";
    private const int Port = 8765;

    [STAThread]
    private static int Main(string[] args)
    {
        string projectRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(
            Path.DirectorySeparatorChar,
            Path.AltDirectorySeparatorChar);
        string pythonExe = Path.Combine(projectRoot, ".venv", "Scripts", "python.exe");
        bool checkOnly = HasArgument(args, "--check");
        bool demoFixtures = HasArgument(args, "--demo");

        if (!File.Exists(pythonExe))
        {
            if (!checkOnly)
            {
                MessageBox.Show(
                    "Python sanal ortami bulunamadi:\n" + pythonExe +
                    "\n\nOnce proje kurulumunu tamamlayin.",
                    "Creator Console baslatilamadi",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
            }
            return 2;
        }

        if (!Directory.Exists(Path.Combine(projectRoot, "creator_console")))
        {
            if (!checkOnly)
            {
                MessageBox.Show(
                    "creator_console klasoru EXE ile ayni proje klasorunde bulunamadi.",
                    "Creator Console baslatilamadi",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
            }
            return 3;
        }

        if (checkOnly)
        {
            return 0;
        }

        string url = "http://" + Host + ":" + Port + "/";
        Process serverProcess = null;

        try
        {
            if (!IsListening(Host, Port))
            {
                ProcessStartInfo startInfo = new ProcessStartInfo
                {
                    FileName = pythonExe,
                    Arguments = "-m creator_console" + (demoFixtures ? " --demo-fixtures" : ""),
                    WorkingDirectory = projectRoot,
                    UseShellExecute = true,
                    WindowStyle = ProcessWindowStyle.Normal,
                };
                serverProcess = Process.Start(startInfo);

                for (int attempt = 0; attempt < 60; attempt++)
                {
                    if (IsListening(Host, Port))
                    {
                        break;
                    }
                    if (serverProcess != null && serverProcess.HasExited)
                    {
                        MessageBox.Show(
                            "Creator Console islemi erken kapandi. Acilan terminaldeki hata mesajini kontrol edin.",
                            "Creator Console baslatilamadi",
                            MessageBoxButtons.OK,
                            MessageBoxIcon.Error);
                        return 4;
                    }
                    Thread.Sleep(250);
                }
            }

            if (!IsListening(Host, Port))
            {
                MessageBox.Show(
                    "Creator Console 15 saniye icinde hazir olmadi. Acilan terminaldeki hata mesajini kontrol edin.",
                    "Creator Console baslatilamadi",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Warning);
                return 5;
            }

            Process.Start(new ProcessStartInfo
            {
                FileName = url,
                UseShellExecute = true,
            });
            return 0;
        }
        catch (Exception exc)
        {
            MessageBox.Show(
                "Creator Console baslatilirken hata olustu:\n" + exc.Message,
                "Creator Console baslatilamadi",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
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

    private static bool IsListening(string host, int port)
    {
        try
        {
            using (TcpClient client = new TcpClient())
            {
                IAsyncResult result = client.BeginConnect(host, port, null, null);
                bool connected = result.AsyncWaitHandle.WaitOne(200);
                if (!connected)
                {
                    return false;
                }
                client.EndConnect(result);
                return client.Connected;
            }
        }
        catch
        {
            return false;
        }
    }
}
