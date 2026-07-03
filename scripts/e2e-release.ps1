#Requires -Version 5.1

param(
    [Parameter(Mandatory)]
    [ValidateSet('no-deps', 'deps')]
    [string]$Kind,

    [string]$Tool = 'autobench',

    [string]$ToolPath = 'D:\Projects\autobench'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:RunId = $null
$script:SourceSha = $null

function Assert-CommandPassed {
    param([Parameter(Mandatory)][string]$Description)

    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Wait-MainCi {
    param([Parameter(Mandatory)][string]$Sha)

    $ciRun = $null
    for ($i = 0; $i -lt 24 -and -not $ciRun; $i++) {
        $ciRun = gh run list `
            --commit $Sha `
            --branch main `
            --workflow CI `
            --json databaseId `
            --limit 1 `
            --jq '.[0].databaseId'
        if (-not $ciRun) {
            Start-Sleep -Seconds 5
        }
    }

    if (-not $ciRun) {
        throw "Post-merge CI did not appear for $Sha"
    }

    gh run watch $ciRun --exit-status
    Assert-CommandPassed "Post-merge CI watch for $Sha"
}

function Get-OpenReleaseRun {
    param([Parameter(Mandatory)][string]$SourceSha)

    $matches = @(
        Get-ChildItem edge-deploy\runs -Directory -ErrorAction SilentlyContinue |
        Where-Object {
            $state = Get-Content "$($_.FullName)\state.json" -Raw |
                ConvertFrom-Json
            $state.status -eq 'open' -and
                $state.source_sha -eq $SourceSha
        }
    )

    if ($matches.Count -ne 1) {
        throw "Expected one open run for $SourceSha; found $($matches.Count)"
    }

    return $matches[0].Name
}

function Get-CompleteReleaseRun {
    param([Parameter(Mandatory)][string]$SourceSha)

    $matches = @(
        Get-ChildItem edge-deploy\runs -Directory -ErrorAction SilentlyContinue |
        Where-Object {
            $state = Get-Content "$($_.FullName)\state.json" -Raw |
                ConvertFrom-Json
            $state.status -eq 'complete' -and
                $state.source_sha -eq $SourceSha
        }
    )

    if ($matches.Count -ne 1) {
        throw "Expected one complete run for $SourceSha; found $($matches.Count)"
    }

    return $matches[0].Name
}

function Resolve-RunIdForFailure {
    param([Parameter(Mandatory)][string]$SourceSha)

    if ($script:RunId) {
        return $script:RunId
    }

    $runsRoot = Join-Path $ToolPath 'edge-deploy\runs'
    if (-not (Test-Path $runsRoot)) {
        return $null
    }

    $candidates = @(
        Get-ChildItem $runsRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object {
            $statePath = Join-Path $_.FullName 'state.json'
            if (-not (Test-Path $statePath)) {
                return $false
            }
            $state = Get-Content $statePath -Raw | ConvertFrom-Json
            $state.source_sha -eq $SourceSha -and
                $state.status -in @('open', 'complete')
        }
    )

    if ($candidates.Count -eq 1) {
        return $candidates[0].Name
    }

    return $null
}

function Show-FailureGuidance {
    param([Parameter(Mandatory)][string]$SourceSha)

    $runId = Resolve-RunIdForFailure -SourceSha $SourceSha
    if (-not $runId) {
        return
    }

    Write-Host ''
    Write-Host '==> Failure guidance (run preserved; do not abandon or delete)' -ForegroundColor Yellow
    Write-Host "Run id: $runId"
    Write-Host "Inspect: python -m edge_deploy status --run $runId"
    Write-Host "Resume:  python -m edge_deploy release --guided --run $runId"
    Write-Host ''
}

function Assert-NoOpenRuns {
    # cmd /c merges stderr without tripping PowerShell 5.1's NativeCommandError
    # under ErrorActionPreference = Stop.
    $statusOutput = (cmd /c 'py -m edge_deploy status 2>&1' | Out-String).TrimEnd()
    if ($LASTEXITCODE -ne 0) {
        throw "Inspect open release runs failed with exit code ${LASTEXITCODE}:`n$statusOutput"
    }

    if ($statusOutput -match '(?m)^no open runs under ') {
        return
    }

    throw @"
Open release run(s) exist. Do not start another release.

$statusOutput

Continue the existing run:
  python -m edge_deploy release --guided --run <run-id>

Or abandon it with a truthful reason:
  python -m edge_deploy abandon --run <run-id> --reason "<reason>"
"@
}

function Assert-EngineVersion {
    $engineVersion = (& py -c 'import edge_deploy; print(edge_deploy.__version__)').Trim()
    Assert-CommandPassed 'Inspect loaded edge-deploy-core version'

    if ($engineVersion -ne '1.2.7') {
        throw @"
Expected edge-deploy-core version 1.2.7; loaded $engineVersion.

Install the tagged release engine (not editable):
  py -m pip install "git+https://github.com/pedrochagasmaster/edge-deploy-core.git@v1.2.7"
"@
    }
}

function Ensure-BbToken {
    if (-not [string]::IsNullOrWhiteSpace($env:BB_TOKEN)) {
        return
    }

    $secureToken = Read-Host 'BB_TOKEN' -AsSecureString
    $env:BB_TOKEN = [System.Net.NetworkCredential]::new('', $secureToken).Password
    Remove-Variable secureToken
}

function Confirm-GitHubPosture {
    param([Parameter(Mandatory)][string]$Reason)

    Write-Host ''
    Write-Host '==> GitHub posture required' -ForegroundColor Yellow
    Write-Host $Reason
    Write-Host 'Switch firewall posture to GitHub now, then press Enter to continue.'
    Read-Host 'Continue'
}

function Get-BranchName {
    param([Parameter(Mandatory)][string]$ReleaseKind)

    $dateStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    return "codex/e2e-$ReleaseKind-$dateStamp"
}

function Apply-NoDepsMarker {
    py -c @"
from pathlib import Path
p = Path('benchmark.py')
marker = b'# E2E release exercise: cosmetic runtime change; no behavior change.'
content = p.read_bytes()
assert marker not in content
p.write_bytes(content.rstrip() + b'\n\n' + marker + b'\n')
"@
    Assert-CommandPassed 'Apply no-deps cosmetic marker to benchmark.py'
}

function Apply-DepsMarker {
    $beforeDependencies = @(
        Get-Content requirements.txt |
        Where-Object { $_ -notmatch '^\s*(#|$)' }
    )

    py -c @"
from pathlib import Path
p = Path('requirements.txt')
marker = b'# E2E release exercise: dependency path changed; dependency set unchanged.'
content = p.read_bytes()
assert marker not in content
p.write_bytes(content.rstrip() + b'\n\n' + marker + b'\n')
"@
    Assert-CommandPassed 'Apply deps cosmetic marker to requirements.txt'

    $afterDependencies = @(
        Get-Content requirements.txt |
        Where-Object { $_ -notmatch '^\s*(#|$)' }
    )

    if (Compare-Object $beforeDependencies $afterDependencies) {
        throw 'Effective dependency declarations changed after requirements.txt marker edit'
    }
}

function Update-ReleaseEnginePin {
    $pinOld = 'edge-deploy-core @ git+https://github.com/pedrochagasmaster/edge-deploy-core.git@v1.1.0'
    $pinNew = 'edge-deploy-core @ git+https://github.com/pedrochagasmaster/edge-deploy-core.git@v1.2.7'
    $pyprojectPath = Join-Path $ToolPath 'pyproject.toml'
    $content = Get-Content $pyprojectPath -Raw

    if ($content -notlike "*$pinOld*") {
        throw "Expected v1.1.0 pin string not found in pyproject.toml: $pinOld"
    }

    $updated = $content.Replace($pinOld, $pinNew)
    Set-Content -Path $pyprojectPath -Value $updated -NoNewline
}

function Get-PullRequestBody {
    param([Parameter(Mandatory)][string]$ReleaseKind)

    if ($ReleaseKind -eq 'no-deps') {
        return @"
## Validation
- powershell -NoProfile -File tools/dev/local_check.ps1

## Release risk
Cosmetic Python comment only. No runtime or dependency behavior changes.
"@
    }

    return @"
## Validation
- powershell -NoProfile -File tools/dev/local_check.ps1
- Effective non-comment requirements compared before and after
- Bumps edge-deploy-core release extra pin from v1.1.0 to v1.2.7

## Release risk
Comment-only requirements.txt change plus release-engine pin bump to v1.2.7.
Dependency resolution is intentionally unchanged, but the dependency delivery
path will run.
"@
}

function Get-CommitMessage {
    param([Parameter(Mandatory)][string]$ReleaseKind)

    if ($ReleaseKind -eq 'no-deps') {
        return 'test: exercise non-dependency release path'
    }

    return 'test: exercise dependency delivery path'
}

function Assert-ReleaseEvidence {
    param(
        [Parameter(Mandatory)][string]$RunId,
        [Parameter(Mandatory)][string]$SourceSha,
        [Parameter(Mandatory)][string]$ReleaseKind
    )

    $statePath = Join-Path $ToolPath "edge-deploy\runs\$RunId\state.json"
    $reportPath = Join-Path $ToolPath "edge-deploy\runs\$RunId\release.json"

    $state = Get-Content $statePath -Raw | ConvertFrom-Json
    $report = Get-Content $reportPath -Raw | ConvertFrom-Json

    if ($state.status -ne 'complete') {
        throw "Release is not complete (status=$($state.status))"
    }

    if ($report.summary.overall -ne 'passed') {
        throw "Release report did not pass (overall=$($report.summary.overall))"
    }

    if ($ReleaseKind -eq 'no-deps') {
        $unexpectedDependencies = @(
            $report.rollouts | Where-Object { $null -ne $_.dependency }
        )
        if ($unexpectedDependencies.Count -ne 0) {
            throw 'Release unexpectedly delivered dependencies on the no-deps path'
        }
    }
    else {
        $dependencyRollouts = @(
            $report.rollouts |
            Where-Object {
                $null -ne $_.dependency -and
                    $_.dependency.source_sha -eq $SourceSha -and
                    -not [string]::IsNullOrWhiteSpace(
                        [string]$_.dependency.manifest.bundle_digest
                    )
            }
        )

        if ($dependencyRollouts.Count -ne $report.rollouts.Count) {
            throw 'Not every rollout recorded valid dependency delivery evidence'
        }
    }

    if (
        $state.phases.tag_github.evidence.tag -ne
        $state.phases.tag_bitbucket.evidence.tag
    ) {
        throw 'Remote GitHub and Bitbucket tag names differ'
    }

    Write-Host ''
    Write-Host '==> Evidence summary' -ForegroundColor Green
    Write-Host "Run id:        $RunId"
    Write-Host "Source SHA:    $SourceSha"
    Write-Host "State status:  $($state.status)"
    Write-Host "Report overall: $($report.summary.overall)"
    Write-Host "Release tag:   $($state.phases.tag_github.evidence.tag)"
    Write-Host ''
    Write-Host 'Per-node evidence:'

    $report.rollouts |
        Select-Object `
            node, `
            status, `
            drift, `
            smoke, `
            @{ Name = 'BundleDigest'; Expression = {
                    if ($null -ne $_.dependency) {
                        [string]$_.dependency.manifest.bundle_digest
                    }
                }
            } |
        Format-Table -AutoSize |
        Out-String |
        ForEach-Object { Write-Host $_.TrimEnd() }
}

try {
    Write-Host '==> Preparation' -ForegroundColor Cyan

    if (-not (Test-Path $ToolPath)) {
        throw "Tool repository path not found: $ToolPath"
    }

    Set-Location $ToolPath

    Confirm-GitHubPosture -Reason 'Preparation reads GitHub origin/main before creating the release exercise branch.'

    git switch main
    Assert-CommandPassed "Switch $Tool to main"

    git pull --ff-only origin main
    Assert-CommandPassed "Update $Tool main"

    if (git status --porcelain --untracked-files=all) {
        throw "$Tool worktree is not clean"
    }

    Assert-EngineVersion

    if (-not (Test-Path "$env:APPDATA\edge-deploy\config.yaml")) {
        throw "Missing operator configuration at $env:APPDATA\edge-deploy\config.yaml"
    }

    Ensure-BbToken
    Assert-NoOpenRuns

    Write-Host 'Preparation complete.' -ForegroundColor Green

    Write-Host ''
    Write-Host '==> Branch and cosmetic change' -ForegroundColor Cyan

    $branchName = Get-BranchName -ReleaseKind $Kind
    $commitMessage = Get-CommitMessage -ReleaseKind $Kind

    git switch -c $branchName
    Assert-CommandPassed "Create branch $branchName"

    if ($Kind -eq 'no-deps') {
        Apply-NoDepsMarker
        $pathsToCommit = @('benchmark.py')
    }
    else {
        Apply-DepsMarker
        Update-ReleaseEnginePin
        $pathsToCommit = @('requirements.txt', 'pyproject.toml')
    }

    Write-Host ''
    Write-Host '==> Local validation' -ForegroundColor Cyan

    powershell -NoProfile -File tools/dev/local_check.ps1
    Assert-CommandPassed 'Local validation (tools/dev/local_check.ps1)'

    git diff --check
    Assert-CommandPassed 'Diff whitespace check'

    Write-Host ''
    Write-Host '==> Pull request' -ForegroundColor Cyan

    git add @pathsToCommit
    git commit -m $commitMessage
    Assert-CommandPassed 'Commit cosmetic change'

    Confirm-GitHubPosture -Reason 'The PR workflow now pushes the branch, creates the GitHub PR, and watches GitHub checks.'

    git push -u origin HEAD
    Assert-CommandPassed 'Push branch'

    $prBody = Get-PullRequestBody -ReleaseKind $Kind

    gh pr create `
        --base main `
        --head $branchName `
        --title $commitMessage `
        --body $prBody
    Assert-CommandPassed 'Create pull request'

    $prNumber = gh pr view --json number --jq '.number'
    Assert-CommandPassed 'Resolve pull request number'

    $prUrl = gh pr view --json url --jq '.url'
    Assert-CommandPassed 'Resolve pull request URL'

    gh pr checks $prNumber --watch --fail-fast
    Assert-CommandPassed 'Pull-request CI'

    Write-Host ''
    Write-Host '========================================' -ForegroundColor Cyan
    Write-Host '  PR READY FOR HUMAN REVIEW' -ForegroundColor Cyan
    Write-Host '========================================' -ForegroundColor Cyan
    Write-Host "Pull request: $prUrl"
    Write-Host ''
    Write-Host 'Review the pull request. When ready to merge, type: merge'
    $approval = Read-Host 'Proceed'
    if ($approval -ne 'merge') {
        Write-Host "Aborted. Pull request remains open: $prUrl"
        exit 0
    }

    Write-Host ''
    Write-Host '==> Merge and post-merge CI' -ForegroundColor Cyan

    Confirm-GitHubPosture -Reason 'Merging the PR and waiting for post-merge CI require GitHub access.'

    gh pr merge $prNumber --squash --delete-branch
    Assert-CommandPassed 'Squash-merge pull request'

    git switch main
    Assert-CommandPassed "Return to $Tool main"

    git pull --ff-only origin main
    Assert-CommandPassed "Update merged $Tool main"

    $script:SourceSha = (git rev-parse HEAD).Trim()
    Assert-CommandPassed 'Resolve merged source SHA'

    Wait-MainCi -Sha $script:SourceSha

    Write-Host ''
    Write-Host '==> Guided release' -ForegroundColor Cyan
    Write-Host 'Running: py -m edge_deploy release --guided'
    Write-Host 'Switch firewall posture when prompted and enter RSA passcodes at each node prompt.'
    Write-Host ''

    py -m edge_deploy release --guided
    if ($LASTEXITCODE -ne 0) {
        try {
            $script:RunId = Get-OpenReleaseRun -SourceSha $script:SourceSha
        }
        catch {
            $script:RunId = $null
        }
        throw "Guided release failed with exit code $LASTEXITCODE"
    }

    Write-Host ''
    Write-Host '==> Resolve completed run' -ForegroundColor Cyan

    $script:RunId = Get-CompleteReleaseRun -SourceSha $script:SourceSha

    py -m edge_deploy status --run $script:RunId
    Assert-CommandPassed 'Inspect completed run status'

    Write-Host ''
    Write-Host '==> Evidence assertions' -ForegroundColor Cyan

    Assert-ReleaseEvidence `
        -RunId $script:RunId `
        -SourceSha $script:SourceSha `
        -ReleaseKind $Kind

    Write-Host ''
    Write-Host 'E2E release complete.' -ForegroundColor Green
}
catch {
    if ($script:SourceSha) {
        Show-FailureGuidance -SourceSha $script:SourceSha
    }
    throw
}
