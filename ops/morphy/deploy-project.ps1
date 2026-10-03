# Установка собственных компонентов в workspace Morphy, без правки аккаунтов.
$ErrorActionPreference = 'Stop'
$morphyProject = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$morphyPackage = Join-Path $morphyProject 'data/morphy-eval/package'
$morphyClient = Join-Path $morphyPackage 'workspace/client/src'
$morphyBackend = Join-Path $morphyPackage 'workspace/backend'
$morphyVersion = (Get-Content -LiteralPath (Join-Path $morphyPackage 'package.json') -Raw | ConvertFrom-Json).version
if ($morphyVersion -ne '0.5.0') { throw 'Патч рассчитан на Morphy 0.5.0; другую версию сначала проверить.' }
foreach ($morphyPair in @(
    @('ProjectDashboard.tsx', (Join-Path $morphyClient 'ProjectDashboard.tsx')),
    @('project.css', (Join-Path $morphyClient 'project.css')),
    @('project-backend.ts', (Join-Path $morphyBackend 'project.ts'))
)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $morphyPair[0]) -Destination $morphyPair[1]
}
$morphyUtf8 = New-Object System.Text.UTF8Encoding($false)
$morphyAppPath = Join-Path $morphyClient 'App.tsx'
$morphyApp = [IO.File]::ReadAllText($morphyAppPath)
if (-not $morphyApp.Contains("import ProjectDashboard from './ProjectDashboard';")) {
    $morphyAnchor = "import DashboardPage from './components/Dashboard/DashboardPage';"
    if (-not $morphyApp.Contains($morphyAnchor)) { throw 'App.tsx изменился: нужен ручной разбор точки подключения.' }
    $morphyApp = $morphyApp.Replace($morphyAnchor, $morphyAnchor + "`nimport ProjectDashboard from './ProjectDashboard';")
    $morphyRoute = '<Route path="/" element={<DashboardPage />} />'
    if (-not $morphyApp.Contains($morphyRoute)) { throw 'Корневой route изменился: файл не записан.' }
    $morphyApp = $morphyApp.Replace($morphyRoute, '<Route path="/" element={<ProjectDashboard />} />' + "`n" + '<Route path="/project" element={<ProjectDashboard />} />' + "`n" + '<Route path="/morphy-dashboard" element={<DashboardPage />} />')
    [IO.File]::WriteAllText($morphyAppPath, $morphyApp, $morphyUtf8)
}
$morphyBackendPath = Join-Path $morphyBackend 'index.ts'
$morphyBackendText = [IO.File]::ReadAllText($morphyBackendPath)
if (-not $morphyBackendText.Contains("import { mountProjectRoutes } from './project.js';")) {
    $morphyBackendAnchor = '// 404 catch-all'
    if (-not $morphyBackendText.Contains($morphyBackendAnchor)) { throw 'Backend изменился: точка подключения не найдена.' }
    $morphyBackendText = "import { mountProjectRoutes } from './project.js';`n" + $morphyBackendText.Replace($morphyBackendAnchor, "mountProjectRoutes(app);`n`n" + $morphyBackendAnchor)
    [IO.File]::WriteAllText($morphyBackendPath, $morphyBackendText, $morphyUtf8)
}
$morphySidebarPath = Join-Path $morphyClient 'components/Layout/Sidebar.tsx'
$morphySidebar = [IO.File]::ReadAllText($morphySidebarPath)
$morphySidebar = $morphySidebar.Replace('label="Dashboard"', 'label="Проект OKX"')
$morphySidebar = $morphySidebar.Replace('to="/app1" icon={AppWindow} label="App 1"', 'to="/project?section=tasks" icon={AppWindow} label="Задачи"')
$morphySidebar = $morphySidebar.Replace('to="/project" icon={AppWindow} label="Доска и интеграции"', 'to="/project?section=tasks" icon={AppWindow} label="Задачи"')
$morphySidebar = $morphySidebar.Replace('to="/research" icon={Search} label="Research"', 'to="/project?section=journal" icon={Search} label="Знания и отчёты"')
$morphySidebar = $morphySidebar.Replace('to="/whatelse" icon={CircleHelp} label="What Else?"', 'to="/project?section=integrations" icon={CircleHelp} label="Все интеграции"')
$morphySidebar = $morphySidebar.Replace('Good Morning', 'Доброе утро').Replace('Good Afternoon', 'Добрый день').Replace('Good Evening', 'Добрый вечер')
$morphySidebar = $morphySidebar.Replace("Workspace: {backendStatus === 'healthy' ? 'Live' : 'Restarting'}", "Приложение: {backendStatus === 'healthy' ? 'Онлайн' : 'Перезапуск'}")
[IO.File]::WriteAllText($morphySidebarPath, $morphySidebar, $morphyUtf8)
'Экран проекта установлен. Morphy обновит frontend и backend через штатный watcher.'
'Открыть: http://127.0.0.1:7480/'
