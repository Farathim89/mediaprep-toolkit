// MediaPrep Toolkit - portable launcher (built by tools/build_exe.py --portable).
//
// The portable exe = this small launcher + a zip of the normal folder build
// appended to its end (PE overlay, ignored by Windows) + a 72-byte trailer:
//   version (16 bytes ASCII, zero padded) | SHA-256 of the zip (32) |
//   zip offset (int64) | zip length (int64) | magic "MPPAYLD1" (8)
//
// Start: <exe folder>\mediaprep-data\runtime\<version>-<hash8>\ holds the
// unpacked build. When its ".complete" marker is there the launcher just
// starts "MediaPrep Toolkit.exe" from it (MEDIAPREP_PORTABLE_DATA = the data
// folder, command line passed through) and exits - only "--selftest" waits
// and returns the app's exit code. Otherwise (first start / new version) it
// shows a small progress window, checks the payload, unpacks it to
// "<name>.tmp", renames that into place and removes older runtime folders.
// Media\ and Data\ are never touched.
//
// Compiled with the .NET Framework 4 csc.exe that ships with Windows (C# 5).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("MediaPrep Toolkit (portable)")]
[assembly: AssemblyDescription("MediaPrep Toolkit - intro/credits remover and media prep tools (portable)")]
[assembly: AssemblyCompany("MediaPrep Toolkit")]
[assembly: AssemblyProduct("MediaPrep Toolkit")]
[assembly: AssemblyCopyright("© 2026 Farathim, MIT")]
// AssemblyVersion / AssemblyFileVersion / AssemblyInformationalVersion come
// from the VersionInfo.cs that build_exe.py writes (from config.APP_VERSION)

namespace MediaPrepPortable
{
    internal sealed class Payload
    {
        public const string Magic = "MPPAYLD1";
        public const int TrailerSize = 72;
        public string Version;
        public byte[] Sha256;
        public long Offset;
        public long Length;

        public string Hash8
        {
            get
            {
                StringBuilder sb = new StringBuilder();
                for (int i = 0; i < 4; i++) sb.Append(Sha256[i].ToString("x2"));
                return sb.ToString();
            }
        }

        public static Payload Read(string exe)
        {
            using (FileStream fs = new FileStream(exe, FileMode.Open, FileAccess.Read,
                                                  FileShare.ReadWrite | FileShare.Delete, 4096))
            {
                if (fs.Length < TrailerSize) return null;
                byte[] t = new byte[TrailerSize];
                fs.Seek(-TrailerSize, SeekOrigin.End);
                int got = 0;
                while (got < TrailerSize)
                {
                    int n = fs.Read(t, got, TrailerSize - got);
                    if (n <= 0) return null;
                    got += n;
                }
                if (Encoding.ASCII.GetString(t, 64, 8) != Magic) return null;
                Payload p = new Payload();
                p.Version = Encoding.ASCII.GetString(t, 0, 16).TrimEnd('\0').Trim();
                p.Sha256 = new byte[32];
                Array.Copy(t, 16, p.Sha256, 0, 32);
                p.Offset = BitConverter.ToInt64(t, 48);
                p.Length = BitConverter.ToInt64(t, 56);
                if (p.Version.Length == 0 || p.Offset <= 0 || p.Length <= 0 ||
                    p.Offset + p.Length > fs.Length - TrailerSize) return null;
                foreach (char c in p.Version)
                    if (!(char.IsLetterOrDigit(c) || c == '.' || c == '-' || c == '_')) return null;
                return p;
            }
        }
    }

    // read-only window [offset, offset+length) of another stream (the zip inside the exe)
    internal sealed class SubStream : Stream
    {
        private readonly Stream inner;
        private readonly long start, length;
        private long pos;

        public SubStream(Stream inner, long start, long length)
        {
            this.inner = inner; this.start = start; this.length = length;
        }
        public override bool CanRead { get { return true; } }
        public override bool CanSeek { get { return true; } }
        public override bool CanWrite { get { return false; } }
        public override long Length { get { return length; } }
        public override long Position
        {
            get { return pos; }
            set { if (value < 0) throw new IOException("negative position"); pos = value; }
        }
        public override int Read(byte[] buffer, int offset, int count)
        {
            long left = length - pos;
            if (left <= 0) return 0;
            if (count > left) count = (int)left;
            inner.Position = start + pos;
            int n = inner.Read(buffer, offset, count);
            pos += n;
            return n;
        }
        public override long Seek(long offset, SeekOrigin origin)
        {
            long p = origin == SeekOrigin.Begin ? offset
                   : origin == SeekOrigin.Current ? pos + offset : length + offset;
            Position = p;
            return pos;
        }
        public override void Flush() { }
        public override void SetLength(long value) { throw new NotSupportedException(); }
        public override void Write(byte[] b, int o, int c) { throw new NotSupportedException(); }
        protected override void Dispose(bool disposing)
        {
            if (disposing) inner.Dispose();
            base.Dispose(disposing);
        }
    }

    internal sealed class UserError : Exception
    {
        public UserError(string msg) : base(msg) { }
    }

    internal static class Program
    {
        internal const string AppName = "MediaPrep Toolkit";
        internal const string AppExe = "MediaPrep Toolkit.exe";
        internal const string DataName = "mediaprep-data";
        internal const string Complete = ".complete";

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool GetDiskFreeSpaceEx(string dir, out ulong freeForUser,
                                                      out ulong total, out ulong totalFree);

        [STAThread]
        private static int Main()
        {
            string exe = Assembly.GetExecutingAssembly().Location;
            string exeDir = Path.GetDirectoryName(Path.GetFullPath(exe));
            string dataDir = Path.Combine(exeDir, DataName);
            string args = PassThroughArgs();
            bool selftest = args.IndexOf("--selftest", StringComparison.OrdinalIgnoreCase) >= 0;

            Payload p;
            try { p = Payload.Read(exe); }
            catch (Exception e) { return Fail("Can't read " + Path.GetFileName(exe) + ":\n" + e.Message); }
            if (p == null)
                return Fail(Path.GetFileName(exe) + " is damaged (its program data is missing). " +
                            "Please download it again.");

            string runtimeDir = Path.Combine(dataDir, "runtime");
            string target = Path.Combine(runtimeDir, p.Version + "-" + p.Hash8);

            // fast path: already unpacked
            if (IsReady(target))
            {
                int rc = Launch(target, dataDir, args, selftest);
                CleanupOld(runtimeDir, Path.GetFileName(target), dataDir, false);
                return rc;
            }
            return Setup(exe, p, dataDir, runtimeDir, target, args, selftest);
        }

        private static bool IsReady(string target)
        {
            return File.Exists(Path.Combine(target, Complete)) &&
                   File.Exists(Path.Combine(target, AppExe));
        }

        // the raw command line minus the exe itself, so quoting stays exactly as typed
        private static string PassThroughArgs()
        {
            string cl = Environment.CommandLine ?? "";
            int i = 0;
            while (i < cl.Length && char.IsWhiteSpace(cl[i])) i++;
            if (i < cl.Length && cl[i] == '"')
            {
                int j = cl.IndexOf('"', i + 1);
                i = j < 0 ? cl.Length : j + 1;
            }
            else
            {
                while (i < cl.Length && !char.IsWhiteSpace(cl[i])) i++;
            }
            return cl.Substring(i).TrimStart();
        }

        private static int Launch(string target, string dataDir, string args, bool wait)
        {
            ProcessStartInfo si = new ProcessStartInfo(Path.Combine(target, AppExe), args);
            si.UseShellExecute = false;
            si.WorkingDirectory = Environment.CurrentDirectory;
            si.EnvironmentVariables["MEDIAPREP_PORTABLE_DATA"] = dataDir;
            try
            {
                using (Process proc = Process.Start(si))
                {
                    if (!wait) return 0;
                    proc.WaitForExit();
                    return proc.ExitCode;
                }
            }
            catch (Exception e)
            {
                return Fail("Can't start " + AppName + ":\n" + e.Message);
            }
        }

        internal static int Fail(string msg)
        {
            MessageBox.Show(msg, AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        private static Mutex OpenMutex(string dataDir)
        {
            // one per data folder (two different portable copies don't block each other)
            string key = dataDir.ToLowerInvariant();
            byte[] h;
            using (SHA256 sha = SHA256.Create()) h = sha.ComputeHash(Encoding.UTF8.GetBytes(key));
            return new Mutex(false, "Local\\MediaPrepPortable-" + BitConverter.ToString(h, 0, 8).Replace("-", ""));
        }

        private static bool Acquire(Mutex m, int ms)
        {
            try { return m.WaitOne(ms); }
            catch (AbandonedMutexException) { return true; }     // an earlier setup was killed
        }

        private static int Setup(string exe, Payload p, string dataDir, string runtimeDir,
                                 string target, string args, bool selftest)
        {
            using (Mutex m = OpenMutex(dataDir))
            {
                if (!Acquire(m, 0))
                {
                    // another start is unpacking right now - wait for it
                    if (!Acquire(m, 30 * 60 * 1000))
                        return Fail("Another start of " + AppName + " is still setting up. Please try again.");
                }
                try
                {
                    if (!IsReady(target))
                    {
                        Application.EnableVisualStyles();
                        Application.SetCompatibleTextRenderingDefault(false);
                        SetupForm form = new SetupForm(exe, p, dataDir, runtimeDir, target);
                        Application.Run(form);
                        if (form.Error != null) return Fail(form.Error);
                        if (!IsReady(target)) return 1;           // cancelled
                    }
                }
                finally { m.ReleaseMutex(); }
            }
            int rc = Launch(target, dataDir, args, selftest);
            CleanupOld(runtimeDir, Path.GetFileName(target), dataDir, false);
            return rc;
        }

        // remove other runtime folders (older releases, half-finished unpacks).
        // A folder still in use can't be renamed away and is kept for next time.
        internal static void CleanupOld(string runtimeDir, string keep, string dataDir, bool locked)
        {
            string[] dirs;
            try { dirs = Directory.GetDirectories(runtimeDir); }
            catch { return; }
            if (dirs.Length <= 1) return;
            Mutex m = null;
            try
            {
                if (!locked)
                {
                    m = OpenMutex(dataDir);
                    if (!Acquire(m, 0)) { m.Dispose(); m = null; return; }
                }
                foreach (string d in dirs)
                {
                    if (string.Equals(Path.GetFileName(d), keep, StringComparison.OrdinalIgnoreCase))
                        continue;
                    string doomed = d;
                    if (!d.EndsWith(".del", StringComparison.OrdinalIgnoreCase))
                    {
                        // Windows lets a folder be renamed while its program runs,
                        // so ask the exe itself: an old version still running keeps
                        // its folder until a later start
                        if (InUse(Path.Combine(d, AppExe))) continue;
                        doomed = d + "." + DateTime.Now.Ticks.ToString() + ".del";
                        try { Directory.Move(d, doomed); }
                        catch { continue; }
                    }
                    DeleteTree(doomed);
                }
            }
            finally
            {
                if (m != null) { m.ReleaseMutex(); m.Dispose(); }
            }
        }

        // a running exe can't be opened for writing (its image is mapped)
        private static bool InUse(string exe)
        {
            if (!File.Exists(exe)) return false;
            try
            {
                using (new FileStream(exe, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { }
                return false;
            }
            catch (FileNotFoundException) { return false; }
            catch (DirectoryNotFoundException) { return false; }
            catch { return true; }
        }

        internal static void DeleteTree(string dir)
        {
            if (!Directory.Exists(dir)) return;
            try { Directory.Delete(dir, true); return; }
            catch { }
            try
            {   // read-only files block Directory.Delete
                foreach (string f in Directory.GetFiles(dir, "*", SearchOption.AllDirectories))
                    try { File.SetAttributes(f, FileAttributes.Normal); } catch { }
                Directory.Delete(dir, true);
            }
            catch { }
        }

        internal static ulong FreeBytes(string dir)
        {
            ulong free, total, totalFree;
            if (GetDiskFreeSpaceEx(dir.EndsWith("\\") ? dir : dir + "\\", out free, out total, out totalFree))
                return free;
            return ulong.MaxValue;
        }
    }

    internal sealed class SetupForm : Form
    {
        private static readonly Color Bg = Color.FromArgb(32, 32, 32);
        private static readonly Color Fg = Color.FromArgb(240, 240, 240);
        private static readonly Color Dim = Color.FromArgb(160, 160, 160);
        private static readonly Color Gold = Color.FromArgb(232, 176, 74);
        private static readonly Color Track = Color.FromArgb(58, 58, 58);

        private readonly string exe, dataDir, runtimeDir, target;
        private readonly Payload payload;
        private readonly Label status;
        private readonly Panel bar;
        private double fraction;
        private bool done;
        public string Error;

        public SetupForm(string exe, Payload p, string dataDir, string runtimeDir, string target)
        {
            this.exe = exe; payload = p;
            this.dataDir = dataDir; this.runtimeDir = runtimeDir; this.target = target;

            Text = "Setting up " + Program.AppName;
            try { Icon = Icon.ExtractAssociatedIcon(exe); } catch { }
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            MinimizeBox = true;
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            AutoScaleDimensions = new SizeF(96F, 96F);
            BackColor = Bg;
            ClientSize = new Size(460, 132);

            Label title = new Label();
            title.Text = "Setting up " + Program.AppName + " " + p.Version + " (first start only)…";
            title.Font = new Font("Segoe UI Semibold", 11F);
            title.ForeColor = Fg;
            title.AutoSize = false;
            title.SetBounds(20, 18, 420, 26);

            status = new Label();
            status.Text = "Checking the program files…";
            status.Font = new Font("Segoe UI", 9F);
            status.ForeColor = Dim;
            status.AutoSize = false;
            status.SetBounds(20, 50, 420, 20);

            bar = new Panel();
            bar.SetBounds(20, 82, 420, 8);
            bar.BackColor = Track;
            bar.Paint += delegate(object s, PaintEventArgs e)
            {
                int w = (int)Math.Round(bar.ClientSize.Width * Math.Max(0.0, Math.Min(1.0, fraction)));
                using (SolidBrush b = new SolidBrush(Gold))
                    e.Graphics.FillRectangle(b, 0, 0, w, bar.ClientSize.Height);
            };

            Label note = new Label();
            note.Text = "Next starts are instant. Files go to: " + DataNameShort();
            note.Font = new Font("Segoe UI", 8F);
            note.ForeColor = Dim;
            note.AutoSize = false;
            note.AutoEllipsis = true;
            note.SetBounds(20, 100, 420, 18);

            Controls.Add(title);
            Controls.Add(status);
            Controls.Add(bar);
            Controls.Add(note);
        }

        private string DataNameShort()
        {
            return Path.Combine(Path.GetFileName(Path.GetDirectoryName(dataDir)) ?? "", Program.DataName);
        }

        protected override void OnShown(EventArgs e)
        {
            base.OnShown(e);
            Thread t = new Thread(Work);
            t.IsBackground = true;
            t.Start();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (!done && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;                          // can't stop half-way; it is quick
                return;
            }
            base.OnFormClosing(e);
        }

        private void Report(string text, double frac)
        {
            try
            {
                BeginInvoke((MethodInvoker)delegate
                {
                    if (text != null) status.Text = text;
                    fraction = frac;
                    bar.Invalidate();
                });
            }
            catch { }
        }

        private void Finish(string error)
        {
            Error = error;
            done = true;
            try { BeginInvoke((MethodInvoker)delegate { Close(); }); } catch { }
        }

        private static bool IsDiskFull(IOException e)
        {
            int hr = Marshal.GetHRForException(e) & 0xFFFF;
            return hr == 112 || hr == 39;                 // ERROR_DISK_FULL / ERROR_HANDLE_DISK_FULL
        }

        private string NotWritable()
        {
            return Program.AppName + " can't write next to itself:\n" +
                   Path.GetDirectoryName(dataDir) + "\n\n" +
                   "Move " + Path.GetFileName(exe) + " to a folder you can write to " +
                   "(e.g. Documents, the Desktop or a USB stick) and start it again.";
        }

        private void Work()
        {
            string tmp = target + ".tmp";
            try
            {
                // writable?
                try
                {
                    Directory.CreateDirectory(runtimeDir);
                    string probe = Path.Combine(runtimeDir, ".write-test");
                    File.WriteAllText(probe, "ok");
                    File.Delete(probe);
                }
                catch (UnauthorizedAccessException) { Finish(NotWritable()); return; }
                catch (IOException e)
                {
                    Finish(IsDiskFull(e) ? DiskFull(0) : NotWritable() + "\n\n(" + e.Message + ")");
                    return;
                }

                // a half-finished earlier unpack, or a broken final folder -> start over
                Program.DeleteTree(tmp);
                if (Directory.Exists(target)) Program.DeleteTree(target);
                if (Directory.Exists(tmp) || Directory.Exists(target))
                {
                    Finish("Can't remove an older half-finished setup folder:\n" + tmp +
                           "\n\nClose " + Program.AppName + " if it is running and try again.");
                    return;
                }

                using (FileStream fs = new FileStream(exe, FileMode.Open, FileAccess.Read,
                                                      FileShare.ReadWrite | FileShare.Delete, 1 << 20))
                {
                    // 1) the payload is intact (catches a truncated / corrupted download)
                    using (SHA256 sha = SHA256.Create())
                    {
                        byte[] buf = new byte[1 << 20];
                        fs.Position = payload.Offset;
                        long left = payload.Length;
                        while (left > 0)
                        {
                            int n = fs.Read(buf, 0, (int)Math.Min(buf.Length, left));
                            if (n <= 0) break;
                            sha.TransformBlock(buf, 0, n, null, 0);
                            left -= n;
                            Report(null, 0.15 * (1.0 - (double)left / payload.Length));
                        }
                        sha.TransformFinalBlock(buf, 0, 0);
                        if (left != 0 || !Same(sha.Hash, payload.Sha256))
                        {
                            Finish(Path.GetFileName(exe) + " is damaged (the check of its program " +
                                   "data failed). Please download it again.");
                            return;
                        }
                    }
                }

                // 2) unpack
                using (SubStream sub = new SubStream(new FileStream(exe, FileMode.Open, FileAccess.Read,
                           FileShare.ReadWrite | FileShare.Delete, 1 << 16), payload.Offset, payload.Length))
                using (ZipArchive zip = new ZipArchive(sub, ZipArchiveMode.Read))
                {
                    long total = 0;
                    int files = 0;
                    foreach (ZipArchiveEntry en in zip.Entries)
                        if (en.Name.Length > 0) { total += en.Length; files++; }
                    ulong free = Program.FreeBytes(runtimeDir);
                    if (free != ulong.MaxValue && free < (ulong)(total + (64L << 20)))
                    {
                        Finish(DiskFull(total));
                        return;
                    }

                    string root = Path.GetFullPath(tmp) + Path.DirectorySeparatorChar;
                    Directory.CreateDirectory(tmp);
                    byte[] buf = new byte[1 << 18];
                    long doneBytes = 0;
                    int count = 0, lastPct = -1;
                    foreach (ZipArchiveEntry en in zip.Entries)
                    {
                        string dest = Path.GetFullPath(Path.Combine(tmp, en.FullName.Replace('/', '\\')));
                        if (!dest.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                            throw new IOException("bad entry " + en.FullName);
                        if (en.Name.Length == 0) { Directory.CreateDirectory(dest); continue; }
                        Directory.CreateDirectory(Path.GetDirectoryName(dest));
                        using (Stream src = en.Open())
                        using (FileStream dst = new FileStream(dest, FileMode.CreateNew, FileAccess.Write,
                                                               FileShare.None, 1 << 16))
                        {
                            if (en.Length > 0) dst.SetLength(en.Length);
                            int n;
                            while ((n = src.Read(buf, 0, buf.Length)) > 0)
                            {
                                dst.Write(buf, 0, n);
                                doneBytes += n;
                                int pct = (int)(100.0 * doneBytes / Math.Max(1, total));
                                if (pct != lastPct)
                                {
                                    lastPct = pct;
                                    Report("Unpacking… " + pct + " %", 0.15 + 0.85 * doneBytes / Math.Max(1, total));
                                }
                            }
                        }
                        try { File.SetLastWriteTime(dest, en.LastWriteTime.DateTime); } catch { }
                        count++;
                    }

                    // 3) verify, mark complete, move into place
                    Report("Finishing…", 1.0);
                    int onDisk = Directory.GetFiles(tmp, "*", SearchOption.AllDirectories).Length;
                    if (count != files || onDisk != files)
                        throw new IOException("unpacked " + onDisk + " of " + files + " files");
                }
                File.WriteAllText(Path.Combine(tmp, Program.Complete),
                                  payload.Version + "\r\n" + DateTime.Now.ToString("s") + "\r\n");
                MoveWithRetry(tmp, target);
                Program.CleanupOld(runtimeDir, Path.GetFileName(target), dataDir, true);
                Finish(null);
            }
            catch (UnauthorizedAccessException)
            {
                Program.DeleteTree(tmp);
                Finish(NotWritable());
            }
            catch (IOException e)
            {
                Program.DeleteTree(tmp);
                Finish(IsDiskFull(e) ? DiskFull(0)
                       : "Setting up " + Program.AppName + " failed:\n" + e.Message);
            }
            catch (Exception e)
            {
                Program.DeleteTree(tmp);
                Finish("Setting up " + Program.AppName + " failed:\n" + e.Message);
            }
        }

        private string DiskFull(long need)
        {
            string s = "Not enough free disk space to set up " + Program.AppName;
            if (need > 0) s += " (it needs about " + ((need >> 20) + 64) + " MB)";
            return s + " in:\n" + dataDir + "\n\nFree some space, or move " +
                   Path.GetFileName(exe) + " to another drive, and start it again.";
        }

        private static void MoveWithRetry(string from, string to)
        {
            for (int i = 0; ; i++)
            {
                try { Directory.Move(from, to); return; }
                catch (IOException)
                {   // a virus scanner may still hold a just-written file for a moment
                    if (i >= 25) throw;
                    Thread.Sleep(200);
                }
                catch (UnauthorizedAccessException)
                {
                    if (i >= 25) throw;
                    Thread.Sleep(200);
                }
            }
        }

        private static bool Same(byte[] a, byte[] b)
        {
            if (a == null || b == null || a.Length != b.Length) return false;
            for (int i = 0; i < a.Length; i++) if (a[i] != b[i]) return false;
            return true;
        }
    }
}
