# Demo window for the live scripted-steps test (tests/test_script_live.py).
# Cyrillic control names (what the UIA tree exposes) plus a custom-painted canvas whose text is NOT in
# the UIA tree: "aVL" until Apply is clicked, then "II" - the same shape as the lead-highlight check.
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = 'AppClicker Demo'
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point(80, 80)
$form.ClientSize = New-Object System.Drawing.Size(560, 330)
$form.TopMost = $true

$name = New-Object System.Windows.Forms.TextBox
$name.Name = 'PatientName'; $name.AccessibleName = 'Имя пациента'
$name.Location = New-Object System.Drawing.Point(16, 16); $name.Size = New-Object System.Drawing.Size(240, 24)

$show = New-Object System.Windows.Forms.CheckBox
$show.Name = 'ShowLead'; $show.Text = 'Показать отведение'
$show.Location = New-Object System.Drawing.Point(16, 50); $show.Size = New-Object System.Drawing.Size(240, 24)

$apply = New-Object System.Windows.Forms.Button
$apply.Name = 'ApplyButton'; $apply.Text = 'Применить'
$apply.Location = New-Object System.Drawing.Point(280, 14); $apply.Size = New-Object System.Drawing.Size(120, 30)

$status = New-Object System.Windows.Forms.Label
$status.Name = 'StatusLabel'; $status.Text = 'Статус: ожидание'
$status.Location = New-Object System.Drawing.Point(280, 54); $status.Size = New-Object System.Drawing.Size(240, 24)

$canvas = New-Object System.Windows.Forms.Panel
$canvas.Name = 'RhythmCanvas'; $canvas.AccessibleName = 'Ритм'
$canvas.BackColor = [System.Drawing.Color]::Black
$canvas.Location = New-Object System.Drawing.Point(16, 100); $canvas.Size = New-Object System.Drawing.Size(520, 200)
$canvas.Tag = 'aVL'
$canvas.Add_Paint({
    param($sender, $e)
    $g = $e.Graphics
    $g.SmoothingMode = 'AntiAlias'
    $g.TextRenderingHint = 'AntiAliasGridFit'
    # a dim "trace" behind the label, so the text is not alone on a flat background
    $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 0, 110, 0), 2)
    $points = @()
    for ($x = 0; $x -le 520; $x += 4) {
        $points += New-Object System.Drawing.PointF($x, (130 + 28 * [math]::Sin($x / 14.0)))
    }
    $g.DrawLines($pen, [System.Drawing.PointF[]]$points)
    $font = New-Object System.Drawing.Font('Segoe UI', 28, [System.Drawing.FontStyle]::Bold)
    $g.DrawString([string]$sender.Tag, $font, [System.Drawing.Brushes]::Lime, 16, 8)
})

$apply.Add_Click({
    $status.Text = 'Статус: применено'
    $canvas.Tag = 'II'
    $canvas.Invalidate()
})

$form.Controls.AddRange(@($name, $show, $apply, $status, $canvas))
[System.Windows.Forms.Application]::Run($form)
