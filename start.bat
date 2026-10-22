@echo off
rem ============================================================
rem  Amane 一键启动脚本 (Windows) - 便携版
rem
rem  工具链内嵌在项目 tools\ 目录, 把整个项目文件夹复制到其它 Windows
rem  机器即可直接运行, 无需预先安装 uv / node / pnpm / just / Python:
rem    tools\uv.exe      包管理与虚拟环境
rem    tools\just.exe    任务编排
rem    tools\node.exe    Node 运行时
rem    tools\pnpm.cmd    pnpm 启动器, 用内嵌 node 运行
rem    tools\pnpm\       pnpm 包本体
rem    tools\python\     uv 托管的 CPython 3.14
rem  tools\ 缺失时自动回退到系统 PATH 中的同名工具。
rem
rem  本文件以 GBK 编码 + CRLF 行尾保存, 开头 chcp 936 保证非中文系统也能正确解析。
rem
rem  维护约束:
rem    1. 一律用 goto 标签分派动作, 不把 echo 放进 if 括号块。
rem       块内未转义的 ) & | < > ^ 会提前闭合块, 报「此时不应有 xxx。」
rem    2. 执行其它 .CMD/.BAT 一律走 call。不加 call 时控制权转移出去不再返回,
rem       主脚本后续行全部被跳过, 表现为「跑了半截就结束」。pnpm 就是 .CMD。
rem    3. 保持 GBK + CRLF。Git Bash 的 sed -i 会吃掉 CRLF, 改完必须补回。
rem    4. 中文文案改动后, 用 temp/build_bat2.py 重新转码生成。
rem       源文件是 temp/start.bat.v3.utf8, 不要直接编辑 GBK 成品。
rem    5. 任何分支失败都必须 pause, 否则双击场景下窗口闪退, 错误被吞掉。
rem    6. 内嵌工具升级走 start.bat bundle, 不要手工只拷一半。
rem    7. echo 里的 ^ 只转义紧跟的一个字符。写 ^-> 时 ^> 没被转义, > 仍是重定向,
rem       会把后面那个路径当输出文件截断。箭头一律用纯文本, 不写尖括号。
rem
rem  用法:
rem    start.bat           双击即生产模式, 构建后由服务端托管 SPA http://127.0.0.1:8000
rem    start.bat start     同无参数, 显式生产模式
rem    start.bat dev       开发环境, API :8000 与前端 :5173 热更新
rem    start.bat setup     仅同步依赖并初始化环境
rem    start.bat build     导出 OpenAPI 并构建前端产物
rem    start.bat check     全量检查: lint / 类型 / 前端 / 测试
rem    start.bat test      运行 pytest
rem    start.bat doctor    诊断: 工具来源 / 依赖 / 端口 逐项体检
rem    start.bat env       仅列出工具来源与版本
rem    start.bat bundle    从本机 PATH 把工具链复制进 tools\ 打包带走
rem
rem  迁移到其它机器:
rem    1. 整目录复制, 保留 web\node_modules 里的软链; 拷不了软链时会自动重装。
rem    2. 依赖 (.venv / node_modules) 与本机路径绑定, 换机器首次运行会自动重建,
rem       需要网络下载包; tools\ 只有可执行文件, 不需要网络。
rem    3. tools\ 已被 .gitignore 忽略, 不走 git 分发, 请用文件复制或压缩包。
rem    4. 放在网络共享 UNC 路径上运行时, 脚本会 pushd 临时映射盘符; 但建议先复制到
rem       本地磁盘再跑, 网络盘上装依赖和跑测试都明显更慢。
rem ============================================================

rem 记录原代码页, 结束时还原; 936 让本文件的中文按 GBK 解析
set "OLDCP="
for /f "tokens=2 delims=:" %%C in ('chcp') do set "OLDCP=%%C"
set "OLDCP=%OLDCP: =%"
chcp 936 >nul 2>nul

setlocal enabledelayedexpansion
rem 统一工作目录: 网络路径 UNC 下 cd /d 会失败, 改用 pushd 临时映射盘符
pushd "%~dp0" >nul 2>nul
if not errorlevel 1 goto :cwd_ok
echo   [错误] 无法进入脚本所在目录, 请把整个项目目录复制到本地磁盘后再运行。
echo   当前目录: %CD%
pause
exit /b 1
:cwd_ok

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "TOOLS=%ROOT%\tools"
set "PNPM_VER=11.28.6"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

echo.
echo   Amane - 环境准备中...
echo   ----------------------------------------

rem ---- 1. 接入内嵌工具链 ----
set "TOOLSRC=系统 PATH"
if not exist "%TOOLS%\uv.exe" goto :tools_system
set "TOOLSRC=内嵌 tools"
set "PATH=%TOOLS%;%PATH%"
set "UV_PYTHON_INSTALL_DIR=%TOOLS%\python"
set "UV_PYTHON_PREFERENCE=only-managed"
if exist "%TOOLS%\python" goto :tools_ready
echo   [提示] tools\python 缺失, uv 首次同步时会自动下载到该目录。
goto :tools_ready

:tools_system
echo   [提示] 未发现 tools 目录, 回退到系统 PATH 中的 uv / node / pnpm / just。

:tools_ready
rem ---- 2. just 在 Windows 上默认用 pwsh 执行 recipe, 缺 pwsh 时回退 5.1 ----
set "JUSTSHELL="
where pwsh >nul 2>nul
if not errorlevel 1 goto :shell_ready
set "JUSTSHELL=--shell powershell --shell-arg -NoProfile --shell-arg -Command"
echo   [提示] 未找到 PowerShell 7, 改用系统 PowerShell 5.1 执行任务。

:shell_ready
rem ---- 3. 定位 just, 优先 PATH 首命中, 其次 tools, 再次 uv tool 安装目录 ----
set "JUST="
for /f "delims=" %%P in ('where just 2^>nul') do if not defined JUST set "JUST=%%P"
if defined JUST goto :have_just
if not exist "%TOOLS%\just.exe" goto :just_userdir
set "JUST=%TOOLS%\just.exe"
goto :have_just
:just_userdir
if not exist "%USERPROFILE%\.local\bin\just.exe" goto :no_just
set "JUST=%USERPROFILE%\.local\bin\just.exe"
goto :have_just
:no_just
echo   [错误] 未找到 just 命令。
echo          修复: start.bat bundle 从本机 PATH 打包, 或 uv tool install rust-just
goto :fail

:have_just
rem ---- 4. 工具链自检 ----
set "MISSING="
where uv >nul 2>nul
if errorlevel 1 set "MISSING=!MISSING! uv"
where node >nul 2>nul
if errorlevel 1 set "MISSING=!MISSING! node"
call pnpm --version >nul 2>nul
if errorlevel 1 set "MISSING=!MISSING! pnpm"
if not defined MISSING goto :toolchain_ok
echo   [错误] 缺少前置工具:!MISSING!
echo          uv/node 可从 tools 目录回退, 也可用 start.bat bundle 重新打包。
goto :fail

:toolchain_ok
rem ---- 5. 不依赖同步的分支先处理 ----
if /i "%ACTION%"=="env"    goto :act_env
if /i "%ACTION%"=="doctor" goto :act_doctor
if /i "%ACTION%"=="bundle" goto :act_bundle
if /i "%ACTION%"=="setup"  goto :act_setup

rem ---- 6. 按需同步依赖: 首次运行, 或依赖因换机器换目录而失效 ----
call :check_deps
if not defined NEED_SETUP goto :deps_ready
echo   [1/2] 依赖需要同步, 首次运行或换了机器, uv 与 pnpm 一起安装, 可能要几分钟...
if not defined REBUILD_VENV goto :setup_web_clean
echo         .venv 已失效, 多半是换了目录或换了电脑, 正在重建...
if exist ".venv" rd /s /q ".venv"
:setup_web_clean
if not defined REBUILD_WEB goto :setup_call
echo         web\node_modules 不完整, 正在清理后重装...
if exist "web\node_modules" rd /s /q "web\node_modules"
:setup_call
set "SETUP_TASK=setup"
where git >nul 2>nul
if errorlevel 1 set "SETUP_TASK=sync"
rem 便携版副本不带 .git, 此时 setup 末尾的 git config 会失败, 降级为 sync
if not exist ".git" set "SETUP_TASK=sync"
call "%JUST%" %JUSTSHELL% !SETUP_TASK!
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" goto :deps_failed
echo   [1/2] 依赖同步完成。
goto :deps_done

:deps_ready
echo   [1/2] 依赖已就绪, 跳过同步。
goto :deps_done

:deps_failed
echo   [错误] 依赖同步失败, 退出码 !RC!, 请检查上方日志。
goto :fail

:deps_done
echo   [2/2] 执行: %ACTION%
echo.

rem ---- 7. 动作分派 ----
if /i "%ACTION%"=="dev"   goto :act_dev
if /i "%ACTION%"=="build" goto :act_build
if /i "%ACTION%"=="start" goto :act_start
if /i "%ACTION%"=="check" goto :act_check
if /i "%ACTION%"=="test"  goto :act_test
echo   [错误] 未知动作: %ACTION%
echo          可用: dev / setup / build / start / check / test / doctor / env / bundle
goto :fail

rem ============================================================
rem  各动作
rem ============================================================

:act_env
echo   [模式] %TOOLSRC%
echo   --- 实际生效的可执行文件 ---
call :which uv
call :which node
call :which pnpm
call :which just
echo   --- 版本 ---
uv --version
node --version
call pnpm --version
"%JUST%" --version
goto :done

:act_build
call "%JUST%" %JUSTSHELL% build
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" goto :task_failed
goto :done

:act_check
call "%JUST%" %JUSTSHELL% check
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" goto :task_failed
goto :done

:act_test
call "%JUST%" %JUSTSHELL% test
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" goto :task_failed
goto :done

:act_setup
echo   正在同步依赖...
call "%JUST%" %JUSTSHELL% setup
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" goto :task_failed
echo   依赖同步完成。
goto :done

rem ---- dev: 先查 8000 是否被占, 被占则直接给处置命令 ----
:act_dev
set "API_BUSY="
for /f "tokens=5" %%I in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do set "API_BUSY=%%I"
if not defined API_BUSY goto :dev_check_web
echo.
echo   [错误] 端口 8000 已被占用, PID !API_BUSY!
echo          多半是上次未退出的服务残留, 先结束它再重新运行:
echo            taskkill /F /PID !API_BUSY!
echo          想看完整端口状态: start.bat doctor
goto :fail

:dev_check_web
set "WEB_BUSY="
for /f "tokens=5" %%I in ('netstat -ano ^| findstr /r /c:":5173 .*LISTENING"') do set "WEB_BUSY=%%I"
if not defined WEB_BUSY goto :dev_launch
echo   [警告] 端口 5173 已被 PID !WEB_BUSY! 占用, Vite 会自动改用下一个端口。
echo          启动后请以 Vite 输出里的 Local 地址为准。

:dev_launch
echo   开发服务器即将启动:
echo     API   http://127.0.0.1:8000
echo     前端  http://localhost:5173
echo   按 Ctrl+C 停止全部服务。
echo.
call "%JUST%" %JUSTSHELL% dev
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" goto :dev_failed
goto :done

:dev_failed
echo.
echo   ----------------------------------------
echo   [错误] 开发服务器未能启动, 退出码 !RC!
echo.
echo   常见原因:
echo     1. 端口 8000 或 5173 被占用
echo     2. 依赖未同步或已损坏, 试运行: start.bat setup
echo     3. 网络或镜像源不可达, uv 或 pnpm 下载失败
echo.
echo   诊断: start.bat doctor
echo   请保留本窗口, 把上方输出发给维护者。
goto :fail

rem ---- start: 生产模式, 只关心 8000 ----
:act_start
set "API_BUSY="
for /f "tokens=5" %%I in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do set "API_BUSY=%%I"
if not defined API_BUSY goto :start_launch
echo.
echo   [错误] 端口 8000 已被占用, PID !API_BUSY!
echo          先结束它再重新运行: taskkill /F /PID !API_BUSY!
goto :fail

:start_launch
echo   生产模式启动, 构建后由服务端托管 SPA: http://127.0.0.1:8000
echo.
call "%JUST%" %JUSTSHELL% start
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" goto :start_failed
goto :done

:start_failed
echo.
echo   [错误] 生产模式启动失败, 退出码 !RC!
echo   常见原因:
echo     1. 前端类型检查或构建失败, 看上方 tsc 或 vite 报错
echo     2. 依赖未同步或已损坏, 试运行: start.bat setup
echo   只想先看界面、不想等构建, 可改用开发模式: start.bat dev
echo   诊断: start.bat doctor
goto :fail

rem ============================================================
rem  bundle: 从本机 PATH 收集工具链到 tools\, 便于整包复制到其它机器
rem ============================================================

:act_bundle
echo   [目标] %TOOLS%
if not exist "%TOOLS%" mkdir "%TOOLS%"
call :copy_from_path uv.exe
call :copy_from_path just.exe
call :copy_from_path node.exe
call :copy_pnpm
call :write_pnpm_cmd
set "UV_PYTHON_INSTALL_DIR=%TOOLS%\python"
echo   --- 内嵌 Python ---
call :bundle_python
echo   --- 产物版本 ---
if exist "%TOOLS%\uv.exe" "%TOOLS%\uv.exe" --version
if exist "%TOOLS%\node.exe" "%TOOLS%\node.exe" --version
set "NODEVER="
if exist "%TOOLS%\node.exe" for /f "usebackq delims=" %%V in (`"%TOOLS%\node.exe" --version`) do set "NODEVER=%%V"
if defined NODEVER if not "!NODEVER:~1,2!"=="24" echo   [注意] 内嵌 node 为 !NODEVER!, 与 CI 的 24.x 不一致, 建议换 node 24 后重新 bundle。
if exist "%TOOLS%\node.exe" if exist "%TOOLS%\pnpm\bin\pnpm.cjs" "%TOOLS%\node.exe" "%TOOLS%\pnpm\bin\pnpm.cjs" --version
if exist "%TOOLS%\just.exe" "%TOOLS%\just.exe" --version
echo   --- 完成 ---
echo   bundle 取 PATH 里的首个命中, 打包后请用 start.bat env 核对版本。
echo   请重新运行 start.bat 让脚本按新工具链工作。
goto :done

:bundle_python
if exist "%TOOLS%\uv.exe" goto :bundle_python_go
echo   [跳过] tools\uv.exe 缺失, 无法安装内嵌 Python。
exit /b 0
:bundle_python_go
"%TOOLS%\uv.exe" python install 3.14 --no-bin
if errorlevel 1 goto :bundle_python_failed
echo   [OK]   内嵌 Python 已就绪
exit /b 0
:bundle_python_failed
echo   [失败] Python 下载失败, 多为网络问题, 可设置 HTTPS_PROXY 后重试。
exit /b 0

rem 用法: call :copy_from_path uv.exe
:copy_from_path
set "SRC="
for %%I in (%~1) do set "SRC=%%~$PATH:I"
if not defined SRC goto :copy_missing
copy /y "!SRC!" "%TOOLS%\%~1" >nul 2>nul
if errorlevel 1 goto :copy_failed
echo   [OK]   %~1  from !SRC!
exit /b 0
:copy_missing
echo   [跳过] %~1 不在 PATH
exit /b 0
:copy_failed
echo   [失败] 复制 %~1 失败, 目标可能被占用
exit /b 0

rem pnpm 从 corepack 缓存取, 版本固定为 PNPM_VER
:copy_pnpm
set "PNPM_SRC=%LOCALAPPDATA%\node\corepack\v1\pnpm\%PNPM_VER%"
if exist "!PNPM_SRC!\bin\pnpm.cjs" goto :copy_pnpm_do
echo   [跳过] 未找到 pnpm !PNPM_VER! 的 corepack 缓存, 请先执行一次 pnpm --version
exit /b 0
:copy_pnpm_do
xcopy /e /i /y /q "!PNPM_SRC!" "%TOOLS%\pnpm" >nul 2>nul
if errorlevel 1 goto :copy_pnpm_failed
echo   [OK]   pnpm !PNPM_VER!  from !PNPM_SRC!
exit /b 0
:copy_pnpm_failed
echo   [失败] 复制 pnpm 失败
exit /b 0

:write_pnpm_cmd
if exist "%TOOLS%\pnpm.cmd" goto :write_pnpm_done
> "%TOOLS%\pnpm.cmd" echo @echo off
>>"%TOOLS%\pnpm.cmd" echo rem Portable pnpm launcher: runs pnpm with the bundled node.exe, no system PATH needed.
>>"%TOOLS%\pnpm.cmd" echo rem Maintain as pure ASCII with CRLF line endings.
>>"%TOOLS%\pnpm.cmd" echo "%%~dp0node.exe" "%%~dp0pnpm\bin\pnpm.cjs" %%*
echo   [OK]   pnpm.cmd 已生成
:write_pnpm_done
exit /b 0

rem ============================================================
rem  doctor: 逐项体检, 输出可直接贴给维护者
rem ============================================================

:act_doctor
echo.
echo   --- 工具来源 ---
echo   模式        %TOOLSRC%
echo   项目根目录  %ROOT%
echo   tools 目录  %TOOLS%
call :which uv
call :which node
call :which pnpm
echo   just        %JUST%
echo.
echo   --- 版本 ---
uv --version
node --version
call pnpm --version
"%JUST%" --version
echo.
echo   --- 任务 shell ---
if defined JUSTSHELL echo   [警告] 未找到 PowerShell 7, 已回退 5.1, 任务失败可能不被捕获
if not defined JUSTSHELL echo   [OK]   PowerShell 7 可用
echo.
echo   --- 内嵌 Python ---
if exist "%TOOLS%\python" echo   [OK]   tools\python 存在
if not exist "%TOOLS%\python" echo   [缺失] tools\python, uv 会下载到该目录
echo.
echo   --- 依赖 ---
call :probe ".venv\Scripts\python.exe" 虚拟环境
call :probe "web\node_modules\vite\package.json" 前端依赖
if exist ".venv\pyvenv.cfg" findstr /b /c:"home =" ".venv\pyvenv.cfg"
echo.
echo   --- uv.lock ---
call :uvlock_state
echo.
echo   --- 端口 ---
call :port_state 8000
call :port_state 5173
echo.
echo   --- 应用自检 ---
if not exist ".venv\Scripts\python.exe" goto :doctor_no_venv
".venv\Scripts\python.exe" -c "import sys, importlib.metadata as m, amane; print('   venv py ' + sys.version.split()[0] + ' | amane ' + m.version('amane') + ' | import ok')"
if errorlevel 1 goto :doctor_import_failed
echo.
echo   --- 体检结束 ---
goto :done

:doctor_no_venv
echo   [缺失] 虚拟环境不存在, 请运行: start.bat setup
echo.
echo   --- 体检结束 ---
goto :done

:doctor_import_failed
echo   [失败] amane 导入失败, 依赖可能来自别的目录, 请运行: start.bat setup
echo.
echo   --- 体检结束 ---
goto :done

rem ============================================================
rem  子程序
rem ============================================================

rem 探测依赖是否可用, 结果写入 NEED_SETUP / REBUILD_VENV / REBUILD_WEB
:check_deps
set "NEED_SETUP="
set "REBUILD_VENV="
set "REBUILD_WEB="
if not exist ".venv\Scripts\python.exe" goto :cd_no_venv
".venv\Scripts\python.exe" -c "import amane" >nul 2>nul
if errorlevel 1 goto :cd_venv_broken
goto :cd_web
:cd_no_venv
set "NEED_SETUP=1"
goto :cd_web
:cd_venv_broken
set "NEED_SETUP=1"
set "REBUILD_VENV=1"
:cd_web
if not exist "web\node_modules\vite\package.json" goto :cd_web_missing
pushd "web"
node -e "require.resolve('vite')" >nul 2>nul
if errorlevel 1 goto :cd_web_broken
popd
exit /b 0
:cd_web_missing
set "NEED_SETUP=1"
exit /b 0
:cd_web_broken
popd
set "NEED_SETUP=1"
set "REBUILD_WEB=1"
exit /b 0

rem 用法: call :which uv   -- 只打印 PATH 中第一个命中的可执行文件
:which
set "WHICH="
for /f "delims=" %%P in ('where %~1 2^>nul') do if not defined WHICH set "WHICH=%%P"
if defined WHICH echo   %~1  = !WHICH!
if not defined WHICH echo   %~1  = 未找到
exit /b 0

rem 用法: call :probe "路径" "描述"
:probe
if exist "%~1" echo   [OK]   %~2
if not exist "%~1" echo   [缺失] %~2  -- 请运行 start.bat setup
exit /b 0

rem 用法: call :uvlock_state
:uvlock_state
rem 便携版副本通常不带 .git, 此时 git diff 会以 129 退出并打印用法, 必须单独判掉
if not exist ".git" goto :uvlock_norepo
where git >nul 2>nul
if errorlevel 1 goto :uvlock_nogit
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 goto :uvlock_norepo
git diff --quiet -- uv.lock >nul 2>nul
if errorlevel 1 goto :uvlock_dirty
echo   [干净] uv.lock 与 HEAD 一致
exit /b 0
:uvlock_norepo
echo   [跳过] 当前目录不是 git 仓库, 不做 uv.lock 比对
exit /b 0
:uvlock_nogit
echo   [跳过] 未安装 git, 无法比对 uv.lock
exit /b 0
:uvlock_dirty
echo   [脏]   uv.lock 已被改写, 通常源于本机镜像源 UV_DEFAULT_INDEX
echo          还原: git checkout -- uv.lock
exit /b 0

rem 用法: call :port_state 8000
:port_state
set "PS_PID="
for /f "tokens=5" %%I in ('netstat -ano ^| findstr /r /c:":%~1 .*LISTENING"') do set "PS_PID=%%I"
if defined PS_PID echo   [占用] 端口 %~1  PID !PS_PID!
if not defined PS_PID echo   [空闲] 端口 %~1
exit /b 0

:task_failed
echo.
echo   [错误] 任务失败, 退出码 !RC!
goto :fail

rem ============================================================
rem  收尾
rem ============================================================

:done
echo.
echo   ----------------------------------------
echo   完成。
if /i "%ACTION%"=="dev"   goto :bye
if /i "%ACTION%"=="start" goto :bye
pause
goto :bye

:fail
echo.
pause
popd >nul 2>nul
if defined OLDCP chcp !OLDCP! >nul 2>nul
endlocal
exit /b 1

:bye
popd >nul 2>nul
if defined OLDCP chcp !OLDCP! >nul 2>nul
endlocal
exit /b 0
