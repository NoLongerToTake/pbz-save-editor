using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class Launcher {
    [STAThread]
    private static void Main() {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "runtime", "pythonw.exe");
        string script = Path.Combine(root, "main.py");
        if (!File.Exists(python) || !File.Exists(script)) {
            MessageBox.Show("请先完整解压便携包，再运行编辑器。请保留 runtime、pbz_editor、data、bin 和 vendor 文件夹。", "PBZ存档编辑器", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        try {
            ProcessStartInfo start = new ProcessStartInfo(python, "\"" + script + "\"");
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
            Process.Start(start);
        } catch (Exception error) {
            MessageBox.Show(error.Message, "PBZ存档编辑器启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
