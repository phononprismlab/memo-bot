; memo-bot 安装脚本（Inno Setup 6）
; 编译：ISCC.exe packaging\memo-bot.iss      （在仓库根跑）
; 产物：packaging\output\memo-bot-setup-v<版本>.exe

#define MyAppName "memo-bot"
#define MyAppPublisher "phononprismlab"
#define MyAppURL "https://github.com/phononprismlab/memo-bot"
; 🔴 所有路径都从**本 .iss 所在目录**推出去，不写死绝对路径 ——
;    这样仓库 clone 到任何地方都能编译（以前写死在工作区根，换台机器就废了）。
;    {#SourcePath} 是 Inno 预处理器给的「本 .iss 所在目录」= <仓库>\packaging
#define PocDir SourcePath
#define RepoDir AddBackslash(SourcePath) + ".."
#define DistDir AddBackslash(SourcePath) + "build\memo-bot"
#define OutDir AddBackslash(SourcePath) + "output"

; 🔴 版本号只有一处真源：仓库里的 memo-bot/VERSION（version.py 的 VERSION 常量跟它同步）。
;    这里直接读那个文件，不再手写 —— 免得改版本时漏改 .iss，也保证**安装包名字里的版本号**
;    和程序自己报的版本永远一致（配置中心「检查更新」拿到的下载链接就是按这个版本号拼的）。
#define MyAppVersion Trim(FileRead(FileOpen(RepoDir + "\VERSION")))

[Setup]
AppId={{9C4B7E21-5D3A-4F18-9B62-7A1E3C5D8F04}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}
; 装到用户目录 → 全程不需要管理员权限（不弹 UAC）
DefaultDirName={localappdata}\Programs\memo-bot
DefaultGroupName=memo-bot
DisableProgramGroupPage=yes
OutputDir={#OutDir}
; 🔴 产物名带版本号 —— 用户手上可能同时躺着好几个版本的安装包，不带版本号根本分不清。
;    这个名字**必须**和 `memo-bot/version.py` 的 ASSET_TMPL 一致（GitHub Release 资产同名），
;    否则配置中心「⬇ 下载新版」的直链会 404。
OutputBaseFilename=memo-bot-setup-v{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 🔴 文件占用：我们自己在 [Code] 里先停任务、再杀进程（见 PrepareToInstall），
;    这里让 Inno 也「直接关掉、别问」—— 双保险。用户报的「安装报错、下方窗口
;    列出一堆 Python」就是 Inno 的「文件正在使用中」框（我们的预杀没赶干净时）。
;    `force` = 不弹框直接关；`RestartApplications=no` = 关掉之后不要 Inno 自作主张
;    重启（重启由我们自己的 RestartAppTask 统一负责，否则会多起一份）。
CloseApplications=force
RestartApplications=no
; 控制面板「应用和功能」里显示的名字 + 图标（用品牌标 installer.ico）
UninstallDisplayName=摸鱼bot
UninstallDisplayIcon={app}\icons\installer.ico
; 安装程序自身的图标 —— 资源管理器里那条怒脸小锦鲤
SetupIconFile={#RepoDir}\assets\icons\installer.ico
DisableWelcomePage=no
AllowNoIcons=yes
; 右键属性 → 详细信息 里那几行。🔴 VersionInfoVersion 必须显式写 ——
; 不写的话 Inno 不会自动从 AppVersion 推，属性里会显示 0.0.0.0。
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoProductTextVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序

[Languages]
Name: "chinese"; MessagesFile: "{#PocDir}\ChineseSimplified.isl"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 图标（美术提供，_make_icons.py 生成，仓库 assets/icons 是唯一来源）——
; 快捷方式 / 卸载项都从 {app}\icons 取
Source: "{#RepoDir}\assets\icons\*.ico"; DestDir: "{app}\icons"; Flags: ignoreversion

[Icons]
; 快捷方式用 pythonw.exe（无控制台窗口），参数交给统一启动器；
; 图标按美术给的映射：配置中心=笔记本锦鲤 / 脑内抽屉=纸箱锦鲤 / 卸载=垃圾桶锦鲤
; 🔴 三合一后只有一个「摸鱼bot」入口（管家 hub.py）：它常驻后台、带托盘图标，
;    配置中心 / 脑内抽屉 / 机器人都从它的托盘菜单进。不再各放一个快捷方式。
Name: "{group}\摸鱼bot"; Filename: "{app}\py\pythonw.exe"; Parameters: """{app}\app\memo-launcher.py"" --hub"; WorkingDir: "{app}\app"; IconFilename: "{app}\icons\installer.ico"; Comment: "常驻后台，托盘里开配置中心 / 脑内抽屉 / 机器人"
Name: "{group}\使用说明"; Filename: "{app}\app\README.md"; IconFilename: "{app}\icons\installer.ico"
Name: "{group}\卸载 memo-bot"; Filename: "{uninstallexe}"; IconFilename: "{app}\icons\uninstall.ico"
; 桌面也放一份 —— 天天要碰的入口，藏在开始菜单里太深
Name: "{autodesktop}\摸鱼bot"; Filename: "{app}\py\pythonw.exe"; Parameters: """{app}\app\memo-launcher.py"" --hub"; WorkingDir: "{app}\app"; IconFilename: "{app}\icons\installer.ico"; Comment: "常驻后台，托盘里开配置中心 / 脑内抽屉 / 机器人"

[Run]
Filename: "{app}\py\pythonw.exe"; Parameters: """{app}\app\memo-launcher.py"" --hub"; Description: "现在打开摸鱼bot"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; 删掉整个安装目录（含运行产物：__pycache__ / 日志）。
; 🔴 数据不在这里 —— 在 %LOCALAPPDATA%\memo-bot（另处），卸载不动它。
Type: filesandordirs; Name: "{app}"


[Code]
const
  TaskName = 'MoyuBot';

{ ── 三个公用小工具 ──────────────────────────────────────────────
   🔴 每条判断都带 `$_.Path -like '<安装目录>\*'` / `$_.Execute -like ...` 这一层。
      源码版跑的是同一个 pythonw.exe、用的是同一个任务名 MoyuBot ——
      无脑杀 / 无脑删会把开发机上正在跑的实例一起干掉。 }

{ 停掉指向本安装目录的「开机自启」计划任务（只有装 / 卸时用）。

  🔴 这是「装的时候报错、下方列出一堆 Python」的**另一半原因**：
     自启任务注册时带了 RestartCount=3 / RestartInterval=1min（任务失败后自动重启）。
     我们刚把管家杀掉，任务计划程序过一会儿又把任务拉起来 —— 新起的 pythonw
     立刻占住 app\py\pythonw.exe，[Files] 就替换不掉，Inno 于是弹出
     「文件正在使用中」并列出那几个 Python 进程；用户只能重启机器绕过。
     先停任务、再杀进程，才不会「边杀边生」。装完 RestartAppTask 会再拉起来。 }
procedure StopAppTask(const DataDir: String);
var
  ResultCode: Integer;
begin
  Exec('powershell.exe',
       '-NoProfile -ExecutionPolicy Bypass -Command ' +
       '"$t = Get-ScheduledTask -TaskName ''' + TaskName + ''' -ErrorAction SilentlyContinue; ' +
       'if ($t -and ($t.Actions | Where-Object { $_.Execute -like ''' + DataDir + '\*'' })) ' +
       '{ Stop-ScheduledTask -TaskName ''' + TaskName + ''' -ErrorAction SilentlyContinue }"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ 杀掉安装目录里跑着的 pythonw / python（机器人和脑内抽屉都是它们跑的）。
  装和卸都要用：不杀它们，它们占着 app\ 下的文件，文件替换不掉、也删不干净。
  快捷方式一律用 pythonw.exe；带上 python.exe 是防有人手动用控制台版跑过。

  🔴 杀完要等它们真的退干净再返回 —— Stop-Process 返回时文件句柄不一定已释放，
     紧接着就开始复制文件的话，照样会撞上「正在使用中」。
     循环重查（最多 40 轮 × 250ms）：第一轮杀掉、第二轮查空就 break，
     正常情况约 250ms 返回，杀不掉的顽固进程最多等 10 秒也不会卡死。
  🔴 匹配条件**两条都算**：
     ① 可执行文件路径在安装目录下（正常安装版跑的就是它）；
     ② 命令行里带 `memo-launcher.py` —— 旧版 / 手动从别处拉起、或用户把
        解释器换成别的 Python 时，① 匹配不到，② 能兜住。
     两条都不误伤开发机上其它无关的 python 进程。 }
procedure KillAppProcesses(const DataDir: String);
var
  ResultCode: Integer;
  Ps: String;
begin
  Ps := '$n = ''' + DataDir + '\*''; ' +
        'for ($i = 0; $i -lt 40; $i++) { ' +
        '$p = Get-CimInstance Win32_Process -Filter "Name=''pythonw.exe'' or Name=''python.exe''" ' +
        '-ErrorAction SilentlyContinue | ' +
        'Where-Object { ($_.ExecutablePath -and $_.ExecutablePath -like $n) -or ' +
        '($_.CommandLine -and $_.CommandLine -like ''*memo-launcher.py*'') }; ' +
        'if (-not $p) { break }; ' +
        '$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; ' +
        'Start-Sleep -Milliseconds 250 }';
  Exec('powershell.exe', '-NoProfile -ExecutionPolicy Bypass -Command "' + Ps + '"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ 删掉指向本安装目录的「开机自启」计划任务。只有卸载时用。 }
procedure RemoveAppTask(const DataDir: String);
var
  ResultCode: Integer;
begin
  Exec('powershell.exe',
       '-NoProfile -ExecutionPolicy Bypass -Command ' +
       '"$t = Get-ScheduledTask -TaskName ''' + TaskName + ''' -ErrorAction SilentlyContinue; ' +
       'if ($t -and ($t.Actions | Where-Object { $_.Execute -like ''' + DataDir + '\*'' })) ' +
       '{ Unregister-ScheduledTask -TaskName ''' + TaskName + ''' -Confirm:$false }"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ 装完把机器人拉回来 —— 刚才为了替换文件把它杀了，不拉回来用户会以为机器人坏了
  （MoyuBot 是登录时启动的，不主动拉就得等下次重启）。

  🔴 只在「本来就有指向本安装目录的 MoyuBot 任务」时才拉：没开开机自启的用户，
     不该因为我们装了个更新就凭空多出一个后台进程。
     唯一会「多事」的情况：用户开了自启、但手动停过机器人 —— 装完它会自己起来，
     去配置中心点一下「停止」即可。这个代价比「机器人静悄悄死掉」小得多。 }
procedure RestartAppTask(const DataDir: String);
var
  ResultCode: Integer;
begin
  Exec('powershell.exe',
       '-NoProfile -ExecutionPolicy Bypass -Command ' +
       '"$t = Get-ScheduledTask -TaskName ''' + TaskName + ''' -ErrorAction SilentlyContinue; ' +
       'if ($t -and ($t.Actions | Where-Object { $_.Execute -like ''' + DataDir + '\*'' })) ' +
       '{ Start-ScheduledTask -TaskName ''' + TaskName + ''' }"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ 🔴 安装前必须先把占着文件的进程清掉 —— 这就是「装的时候提示 Python 文件被占用、
  安装失败」的根因：[Files] 要覆盖 app\py\pythonw.exe，而它正被跑着的
  机器人 / 脑内抽屉锁着。

  为什么挂 PrepareToInstall、而不是 CurStepChanged(ssInstall)：
  Inno 官方文档写明 PrepareToInstall「在 Setup 检查文件是否被占用之前」调用
  （原文：This function is called before Setup checks for files being in-use
  if CloseApplications is set to yes）。挂 ssInstall 就晚了 —— 用户会先看到那个
  「文件正在使用中」的框；非静默安装时 Inno 拿 Restart Manager 去关，
  关不掉 pythonw 就直接报错退出，这正是「安装失败」的来源。
  我们先把它杀掉，Inno 就没得可问了。 }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { 🔴 顺序不能反：先停任务（断掉「失败自动重启」这条生路），再杀进程。 }
  StopAppTask(ExpandConstant('{app}'));
  KillAppProcesses(ExpandConstant('{app}'));
  Result := '';
end;

{ 文件铺完 → 把机器人拉回来。[Run] 段随后会打开配置中心。 }
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RestartAppTask(ExpandConstant('{app}'));
end;

{ 🔴 兜底：用户**中途取消**安装时，任务已经被我们停掉了 —— 不补这一下，
   「开机自启」就平白失效了（任务还在，只是停着，下次登录也不会跑）。
   DeinitializeSetup 在成功 / 取消 / 异常退出时都会走，是唯一稳的落点。
   装成功时 RestartAppTask 已经拉过一次，这里再调是幂等的。 }
procedure DeinitializeSetup();
begin
  RestartAppTask(ExpandConstant('{app}'));
end;

{ 卸载前干两件事：
  ① 停掉安装目录里跑着的 pythonw（机器人 / 脑内抽屉）—— 否则它占着 app\ 下的文件，
     卸载删不掉，会留下一堆残骸；
  ② 删掉「开机自启」的计划任务 —— 但**只删指向本安装目录的那个**。 }
function InitializeUninstall(): Boolean;
begin
  StopAppTask(ExpandConstant('{app}'));
  KillAppProcesses(ExpandConstant('{app}'));
  RemoveAppTask(ExpandConstant('{app}'));
  Result := True;
end;

{ 卸载完成后告诉用户数据在哪 —— 卸载不动数据，想清干净得自己删。 }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then Exit;
  if UninstallSilent then Exit;
  DataDir := ExpandConstant('{localappdata}\memo-bot');
  if DirExists(DataDir) then
    MsgBox('卸载完成。' + #13#10 + #13#10 +
           '你的数据（待办 / 灵感 / 配置）还留在这个文件夹里：' + #13#10 +
           DataDir + #13#10 + #13#10 +
           '想彻底清干净，手动删掉它就行。', mbInformation, MB_OK);
end;
