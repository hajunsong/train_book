$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.RandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]

$getAwaiter = [WindowsRuntimeSystemExtensions].GetMember('GetAwaiter').Where({
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    }, 'First')[0]

function Await {
    param($AsyncTask, [Type]$ResultType)
    $getAwaiter.MakeGenericMethod($ResultType).Invoke($null, @($AsyncTask)).GetResult()
}

$engine = $null
foreach ($tag in @('ko-KR', 'ko', 'en-US')) {
    try {
        $lang = [Windows.Globalization.Language]::new($tag)
        if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) {
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
            if ($engine) { break }
        }
    } catch {}
}
if (-not $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if (-not $engine) {
    [Console]::Out.WriteLine('READY:{"ok":false,"error":"no ocr engine"}')
    [Console]::Out.Flush()
    exit 1
}

$langs = @()
foreach ($l in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
    $langs += $l.LanguageTag
}
$ready = @{ ok = $true; lang = $engine.RecognizerLanguage.LanguageTag; available = $langs } | ConvertTo-Json -Compress
[Console]::Out.WriteLine("READY:$ready")
[Console]::Out.Flush()

function Recognize-File([string]$Path) {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bmp = Await ($decoder.GetSoftwareBitmapAsync(
                [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
                [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied
            )) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $ocr = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
        $words = New-Object System.Collections.Generic.List[object]
        foreach ($line in $ocr.Lines) {
            foreach ($word in $line.Words) {
                $r = $word.BoundingRect
                $words.Add([ordered]@{
                        t = $word.Text
                        x = [int][Math]::Round($r.X)
                        y = [int][Math]::Round($r.Y)
                        w = [int][Math]::Round($r.Width)
                        h = [int][Math]::Round($r.Height)
                    })
            }
        }
        return @{ text = [string]$ocr.Text; words = $words }
    } finally {
        $stream.Dispose()
    }
}

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq '' ) { continue }
    if ($line -eq 'quit') { break }
    try {
        if (-not (Test-Path -LiteralPath $line)) {
            throw "file not found: $line"
        }
        $result = Recognize-File $line
        $json = $result | ConvertTo-Json -Compress -Depth 6
        [Console]::Out.WriteLine("OCR:$json")
        [Console]::Out.Flush()
    } catch {
        $err = @{ error = $_.Exception.Message } | ConvertTo-Json -Compress
        [Console]::Out.WriteLine("OCR:$err")
        [Console]::Out.Flush()
    }
}
