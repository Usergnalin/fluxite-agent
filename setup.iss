[Setup]
AppName=Fluxite Agent
AppVersion={#AppVersion}
DefaultDirName={pf}\FluxiteAgent
DefaultGroupName=Fluxite Agent
UninstallDisplayIcon={app}\fluxite-agent.exe
Compression=lzma2
SolidCompression=yes
OutputDir=.
PrivilegesRequired=admin
LicenseFile=eula.txt  

[Files]
Source: "wireguard-x86-1.1.msi";   DestDir: "{tmp}"; Flags: deleteafterinstall; Check: IsX86
Source: "wireguard-amd64-1.1.msi"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: IsX64
Source: "wireguard-arm64-1.1.msi"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: IsARM64
Source: "fluxite-agent.exe"; DestDir: "{app}"; Flags: ignoreversion

[Code]
var
  ConfigPage: TInputQueryWizardPage;

function IsX86: Boolean;
begin
  Result := not IsX64Compatible and not IsARM64;
end;

function IsX64: Boolean;
begin
  Result := IsX64Compatible and not IsARM64;
end;

function WireGuardMsi: String;
begin
  if IsARM64 then
    Result := 'wireguard-arm64-1.1.msi'
  else if IsX64Compatible then
    Result := 'wireguard-amd64-1.1.msi'
  else
    Result := 'wireguard-x86-1.1.msi';
end;

procedure InitializeWizard;
begin
  ConfigPage := CreateInputQueryPage(wpWelcome,
    'Fluxite Agent Configuration',
    'Enter your Linking Code and optional Agent Name',
    'The Linking Code is required to authenticate this device. ' +
    'Get your linking code from the web panel.'#13#10#13#10 +
    'Agent Name is optional and appears in the web panel. ' +
    'Defaults to computer name if left blank.');

  ConfigPage.Add('Linking Code (Required):', False);
  ConfigPage.Add('Agent Name (Optional):', False);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ConfigPage.ID then
  begin
    if ConfigPage.Values[0] = '' then
    begin
      MsgBox('Linking Code is required. Please enter it to continue.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir: String;
  ResultCode: Integer;
  AgentName: String;
  I: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    // ── Data directory ────────────────────────────────────────────────────────
    DataDir := ExpandConstant('{commonappdata}\FluxiteAgent');
    ForceDirectories(DataDir);

    // NetworkService needs Modify (not Full Control) — it reads creds and
    // writes token cache. Full Control is excessive for a network-facing service.
    Exec('icacls',
      '"' + DataDir + '" /grant "NT AUTHORITY\NETWORK SERVICE":(OI)(CI)M /T /Q',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    // ── WireGuard ─────────────────────────────────────────────────────────────
    Exec('msiexec',
      '/i "' + ExpandConstant('{tmp}\') + WireGuardMsi + '" /qn /norestart',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    // Wait for WireGuard driver to register
    for I := 1 to 15 do
    begin
      Exec('sc', 'query WireGuardManager',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      if ResultCode = 0 then Break;
      Sleep(1000);
    end;

    // ── Elevated first-run setup ──────────────────────────────────────────────
    // Runs synchronously as admin (child of this installer process).
    // Handles: API linking, WireGuard tunnel install, JDK downloads,
    //          java firewall rules, mod loader downloads.
    AgentName := ConfigPage.Values[1];
    if AgentName = '' then
      AgentName := GetComputerNameString;
    Exec(ExpandConstant('{app}\fluxite-agent.exe'),
    'setup ' +
    AddQuotes(Trim(ConfigPage.Values[0])) + ' ' +
    AddQuotes(AgentName),
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    if ResultCode <> 0 then
    begin
      MsgBox('Fluxite Agent setup did not complete (exit code ' + IntToStr(ResultCode) + ').'#13#10#13#10 + 'Check C:\ProgramData\FluxiteAgent\logs/latest.log for details.', mbError, MB_OK);
      // Don't install the service, setup failed
      Exit;
    end;

    // ── Service ───────────────────────────────────────────────────────────────
    // Runs as NetworkService — no admin rights, no SCM rights, no WireGuard
    // rights. Setup is complete so the service never needs elevation.
    Exec('sc', 'stop FluxiteAgentService',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('sc', 'delete FluxiteAgentService',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    // Wait up to 10s for the service to stop
    for I := 1 to 10 do
    begin
      Exec('sc', 'query FluxiteAgentService',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      if ResultCode <> 0 then Break;
      Sleep(1000);
    end;
    Exec('sc',
      'create FluxiteAgentService ' +
      'binPath= "' + ExpandConstant('{app}\fluxite-agent.exe') + '" ' +
      'start= auto ' +
      'obj= "NT AUTHORITY\NetworkService"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    if ResultCode <> 0 then
    begin
      MsgBox('Failed to create Fluxite service (code ' + IntToStr(ResultCode) + ').' + #13#10 + 'Try reinstalling as administrator.', mbError, MB_OK);
      Exit;
    end;

    Exec('sc',
      'failure FluxiteAgentService reset= 60 ' +
      'actions= restart/60000/restart/60000/restart/60000',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    Exec('sc', 'start FluxiteAgentService',
      '', SW_HIDE, ewNoWait, ResultCode);
  end;
end;

// ── Uninstall ─────────────────────────────────────────────────────────────────

function InitializeUninstall(): Boolean;
begin
  Result := True;
  MsgBox(
    'Before uninstalling Fluxite Agent, please note:' #13#10#13#10
    'Your Minecraft servers will NOT be stopped automatically. '
    + 'If you want to stop them cleanly, please do so from the Fluxite panel before continuing.'#13#10#13#10
    'Your server worlds and Java runtimes will be kept at:'#13#10
    '  C:\ProgramData\FluxiteAgent\servers'#13#10
    '  C:\ProgramData\FluxiteAgent\runtimes'#13#10#13#10
    'You can delete these manually after uninstalling if you no longer need them.',
    mbInformation, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Stop and remove the agent service
    Exec('sc', 'stop FluxiteAgentService',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(2000); // brief grace before force kill
    Exec('taskkill', '/F /IM fluxite-agent.exe',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('sc', 'delete FluxiteAgentService',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(2000); // brief grace for task kill

    Exec(ExpandConstant('{app}\fluxite-agent.exe'),'cleanup',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    // Wipe all agent data, keep servers and runtimes
    // MC servers keep running
    DelTree(ExpandConstant('{commonappdata}\FluxiteAgent\logs'), True, True, True);
    DelTree(ExpandConstant('{commonappdata}\FluxiteAgent\installers'), True, True, True);
    DelTree(ExpandConstant('{commonappdata}\FluxiteAgent\tmp'), True, True, True);
    DeleteFile(ExpandConstant('{commonappdata}\FluxiteAgent\agent_id.txt'));
    DeleteFile(ExpandConstant('{commonappdata}\FluxiteAgent\agent.key'));
    DeleteFile(ExpandConstant('{commonappdata}\FluxiteAgent\servers.json'));
    DeleteFile(ExpandConstant('{commonappdata}\FluxiteAgent\wgfluxite.conf'));

    MsgBox(
      'Fluxite Agent has been uninstalled.'#13#10#13#10
      'Your Minecraft worlds and Java runtimes are still at:'#13#10
      '  C:\ProgramData\FluxiteAgent\servers'#13#10
      '  C:\ProgramData\FluxiteAgent\runtimes'#13#10#13#10
      'Any servers that were running will continue until stopped or the machine restarts. '
      + 'Delete the folders above manually if you no longer need them.',
      mbInformation, MB_OK);
  end;
end;