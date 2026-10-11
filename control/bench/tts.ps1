param([string]$Manifest, [string]$OutDir)
Add-Type -AssemblyName System.Speech
New-Item -ItemType Directory -Force $OutDir | Out-Null
$items = Get-Content $Manifest -Raw | ConvertFrom-Json
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
Register-ObjectEvent -InputObject $s -EventName SpeakProgress -SourceIdentifier prog | Out-Null
foreach ($it in $items) {
    $wav = Join-Path $OutDir ("p{0:D4}.wav" -f $it.id)
    $log = Join-Path $OutDir ("p{0:D4}.log" -f $it.id)
    if (Test-Path $log) { continue }
    $s.SelectVoice($it.voice)
    $s.Rate = [int]$it.rate
    $s.SetOutputToWaveFile($wav)
    $s.Speak($it.text)
    $s.SetOutputToNull()
    $lines = @()
    foreach ($e in (Get-Event -SourceIdentifier prog)) {
        $a = $e.SourceEventArgs
        $lines += ("{0:N3}`t{1}`t{2}" -f $a.AudioPosition.TotalSeconds, $a.CharacterPosition, $a.Text)
    }
    Remove-Event -SourceIdentifier prog -ErrorAction SilentlyContinue
    $lines | Set-Content $log -Encoding utf8
}
$s.Dispose()
