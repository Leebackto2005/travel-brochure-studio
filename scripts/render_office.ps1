param([Parameter(Mandatory=$true)][string]$InputPath,[Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference='Stop'
$inputFile=(Resolve-Path -LiteralPath $InputPath).Path
$outputFolder=[System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $outputFolder | Out-Null
$application=New-Object -ComObject PowerPoint.Application
$presentation=$null
$script:issues=[System.Collections.Generic.List[string]]::new()
function Check-Shape($shape,$page) {
 if($shape.Type -eq 6){foreach($child in $shape.GroupItems){Check-Shape $child $page}}
 if($shape.HasTextFrame -eq -1 -and $shape.TextFrame2.HasText -eq -1){
  $frame=$shape.TextFrame2
  $availH=$shape.Height-$frame.MarginTop-$frame.MarginBottom
  $availW=$shape.Width-$frame.MarginLeft-$frame.MarginRight
  if($frame.TextRange.BoundHeight -gt $availH+1.5 -or $frame.TextRange.BoundWidth -gt $availW+1.5){$script:issues.Add("P${page}: text overflow: $($shape.Name)")}
 }
 if($shape.HasTable -eq -1){for($row=1;$row -le $shape.Table.Rows.Count;$row++){for($col=1;$col -le $shape.Table.Columns.Count;$col++){Check-Shape $shape.Table.Cell($row,$col).Shape $page}}}
}
try {
 $presentation=$application.Presentations.Open($inputFile,-1,0,0)
 $height=[int](1260*$presentation.PageSetup.SlideHeight/$presentation.PageSetup.SlideWidth)
 for($i=1;$i -le $presentation.Slides.Count;$i++){
  $slide=$presentation.Slides.Item($i)
  $slide.Export((Join-Path $outputFolder ('P{0:D2}.png' -f $i)),'PNG',1260,$height)
  foreach($shape in $slide.Shapes){Check-Shape $shape $i}
 }
 $presentation.SaveAs((Join-Path $outputFolder 'brochure.pdf'),32)
 [pscustomobject]@{source_sha256=(Get-FileHash -LiteralPath $inputFile -Algorithm SHA256).Hash;slides=$presentation.Slides.Count;overflow=@($script:issues.ToArray());renderer='Microsoft PowerPoint';visual_review='required'} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $outputFolder 'render-report.json') -Encoding UTF8
 Write-Output "Rendered $($presentation.Slides.Count) pages to $outputFolder"
} finally {
 if($null -ne $presentation){$presentation.Close();[void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($presentation)}
 [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($application)
}
