function Select-HHResume {
    param([Parameter(Mandatory = $true)][string]$Python, [Parameter(Mandatory = $true)][string]$Repo)

    # The Python helper emits only title and ID, and allows only the resume-list GET
    # (plus token refresh and an explicitly requested manual CAPTCHA).
    # CAPTCHA notices go to stderr so stdout remains valid JSON.
    # Keep the IDs out of the displayed menu.
    $listing = & $Python (Join-Path $Repo 'windows\list_resumes.py')
    if ($LASTEXITCODE -ne 0) { throw 'Could not list HH resumes; nothing was sent.' }
    # In Windows PowerShell 5.1, @(ConvertFrom-Json ...) wraps the *whole*
    # JSON array as one entry. Assign it first, then normalize the result.
    $resumes = ConvertFrom-Json -InputObject $listing
    $resumes = @($resumes)
    return Choose-HHResume -Resumes $resumes
}

function Choose-HHResume {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][array]$Resumes)

    if ($Resumes.Count -eq 0) { throw 'No published HH resumes; nothing was sent.' }

    Write-Host 'Choose a published resume:'
    for ($i = 0; $i -lt $Resumes.Count; $i++) {
        Write-Host ('{0}. {1}' -f ($i + 1), $Resumes[$i].title)
    }
    $answer = Read-Host 'Resume number'
    $number = 0
    if ($answer -notmatch '^[1-9][0-9]*$' -or -not [int]::TryParse($answer, [ref]$number) -or
        $number -gt $Resumes.Count) {
        throw 'Invalid resume number; nothing was sent.'
    }
    return $Resumes[$number - 1].id
}

function Resolve-HHSearchText {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string]$ResumeId,
        [string]$OverrideText
    )

    if ($null -ne $OverrideText -and $OverrideText -ne '') {
        if ([string]::IsNullOrWhiteSpace($OverrideText)) { throw 'Search text cannot be blank.' }
        $search = $OverrideText
    } else {
        $file = Join-Path $Repo 'windows\resume-search.json'
        $mapped = Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json
        $entry = $mapped.PSObject.Properties[$ResumeId]
        if ($null -eq $entry -or [string]::IsNullOrWhiteSpace($entry.Value)) {
            throw 'No saved SearchText for this resume. Pass -SearchText explicitly; nothing was sent.'
        }
        $search = $entry.Value
    }
    Write-Host ('SearchText: {0}' -f $search)
    return $search
}
