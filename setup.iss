[Setup]
AppName=Fluxite Agent
AppVersion=1.0
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
      MsgBox('Linking Code is required. Please enter it to continue.',
             mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir: String;
  ResultCode: Integer;
  AgentName: String;
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

    if (ResultCode <> 0) and (ResultCode <> 1638) then
    begin
      // 1638 = another version already installed — acceptable, continue
      MsgBox('WireGuard installation failed (code ' + IntToStr(ResultCode) + ').' +
        #13#10 + 'The agent cannot function without WireGuard.',
        mbError, MB_OK);
      Exit;
    end;

    // Wait for WireGuard driver to register
    var I: Integer;
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
      MsgBox(
        'Fluxite Agent setup did not complete (exit code ' +
        IntToStr(ResultCode) + ').'#13#10#13#10 +
        'Check C:\ProgramData\FluxiteAgent\fluxite.log for details.'#13#10 +
        'To retry, run "fluxite-agent.exe setup <linking-code>" as administrator.',
        mbError, MB_OK);
      // Don't install the service — nothing is ready to run
      Exit;
    end;

    // ── Service ───────────────────────────────────────────────────────────────
    // Runs as NetworkService — no admin rights, no SCM rights, no WireGuard
    // rights. Setup is complete so the service never needs elevation.
    Exec('sc', 'stop FluxiteAgentService',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('sc', 'delete FluxiteAgentService',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(1000); // give SCM time to process the delete
    Exec('sc',
      'create FluxiteAgentService ' +
      'binPath= "' + ExpandConstant('{app}\fluxite-agent.exe') + '" ' +
      'start= auto ' +
      'obj= "NT AUTHORITY\NetworkService"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    Exec('sc',
      'failure FluxiteAgentService reset= 60 ' +
      'actions= restart/60000/restart/60000/restart/60000',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    Exec('sc', 'start FluxiteAgentService',
      '', SW_HIDE, ewNoWait, ResultCode);
  end;
end;

// ── Uninstall ─────────────────────────────────────────────────────────────────
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Stop and remove the agent service
    Exec('sc', 'stop FluxiteAgentService',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('sc', 'delete FluxiteAgentService',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    Exec(ExpandConstant('{app}\fluxite-agent.exe'),'cleanup ',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    // Wipe all agent data (creds, runtimes, logs, JDKs, mod loaders)
    DelTree(ExpandConstant('{commonappdata}\FluxiteAgent'), True, True, True);
  end;
end;