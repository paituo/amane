@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ============================================================
rem  Amane 一键启动脚本 (Windows)
rem  本文件以 GBK 编码 + CRLF 行尾保存, 匹配 cmd 默认代码页 936
rem
rem  维护约束:
rem    1. 一律用 goto 标签分派动作, 不把 echo 放进 if 括号块。
rem       块内未转义的 ) & | < > ^ 会提前闭合块, 报「此时不应有 xxx。」
rem    2. 执行其它 .CMD/.BAT 一律走 call。不加 call 时控制权转移出去不再返回,
rem       主脚本后续行全部被跳过, 表现为「跑了半截就结束」。pnpm 就是 .CMD。
rem    3. 保持 GBK + CRLF。Git Bash 的 sed -i 会吃掉 CRLF, 改完必须补回。
rem    4. 中文文案改动后, 用 temp/build_bat.py 重新转码生成。
rem    5. 任何分支失败都必须 pause, 否则双击场景下窗口闪退, 错误被吞掉。
rem
rem  用法:
rem    start.bat           启动开发环境, API :8000 与前端 :5173
rem    start.bat setup     仅同步依赖并初始化环境
rem    start.bat build     导出 OpenAPI 并构建前端产物
rem    start.bat start     构建后以生产模式启动服务端 :8000
rem    start.bat check     全量检查: lint / 类型 / 前端 / 测试
rem    start.bat test      运行 pytest
rem    start.bat doctor    诊断: 工具链 / 依赖 / 端口 逐项体检
rem    start.bat env       仅检查本机工具链
rem ============================================================

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=dev"

echo.
echo   Amane - 环境准备中...
echo   ----------------------------------------

rem ---- 1. 定位 just, uv tool 安装在 %USERPROFILE%\.local\bin ----
set "JUST=just"
where just >nul 2>nul
if not errorlevel 1 goto :have_just
if exist "%USERPROFILE%\.local\bin\just.exe" goto :fallback_just
echo   [错误] 未找到 just 命令。
echo          请先安装: uv tool install rust-just
goto :fail
:fallback_just
set "JUST=%USERPROFILE%\.local\bin\just.exe"

:have_just
rem ---- 2. 工具链自检 ----
set "MISSING="
where uv   >nul 2>nul || set "MISSING=!MISSING! uv"
where pnpm >nul 2>nul || set "MISSING=!MISSING! pnpm"
if not defined MISSING goto :toolchain_ok
echo   [错误] 缺少前置工具:!MISSING!
echo          uv:   https://docs.astral.sh/uv/
echo          pnpm: https://pnpm.io/installation
goto :fail

:toolchain_ok
rem ---- 3. 不依赖同步的分支先处理 ----
if /i "%ACTION%"=="env"    goto :act_env
if /i "%ACTION%"=="doctor" goto :act_doctor
if /i "%ACTION%"=="setup"  goto :act_setup

rem ---- 4. 按需同步依赖, 首次运行或依赖缺失时执行 ----
set "NEED_SETUP="
if not exist ".venv\Scripts\python.exe" set "NEED_SETUP=1"
if not exist "web\node_modules"         set "NEED_SETUP=1"
if not defined NEED_SETUP goto :deps_ready
echo   [1/2] 首次运行, 正在同步依赖, uv 与 pnpm 依赖一起安装, 可能需要几分钟...
call "%JUST%" setup
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

rem ---- 5. 动作分派 ----
if /i "%ACTION%"=="dev"   goto :act_dev
if /i "%ACTION%"=="build" goto :act_build
if /i "%ACTION%"=="start" goto :act_start
if /i "%ACTION%"=="check" goto :act_check
if /i "%ACTION%"=="test"  goto :act_test
echo   [错误] 未知动作: %ACTION%
echo          可用: dev / setup / build / start / check / test / doctor / env
goto :fail

rem ============================================================
rem  各动作
rem ============================================================

:act_env
echo   [OK] uv / pnpm / just 均可用
echo   [OK] just 版本:
"%JUST%" --version
goto :done

:act_build
call "%JUST%" build
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" goto :task_failed
goto :done

:act_check
call "%JUST%" check
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" goto :task_failed
goto :done

:act_test
call "%JUST%" test
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" goto :task_failed
goto :done

:act_setup
echo   正在同步依赖...
call "%JUST%" setup
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
call "%JUST%" dev
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
echo     3. uv 同步失败, 通常是网络或镜像源不可达
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
call "%JUST%" start
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" goto :start_failed
goto :done

:start_failed
echo.
echo   [错误] 生产模式启动失败, 退出码 !RC!
echo   诊断: start.bat doctor
goto :fail

rem ============================================================
rem  doctor: 逐项体检, 输出可直接贴给维护者
rem ============================================================

:act_doctor
echo.
echo   --- 工具链 ---
echo   uv:
uv --version
echo   pnpm:
call pnpm --version
echo   node:
node --version
echo   just:
"%JUST%" --version
echo.
echo   --- 路径 ---
echo   just  %JUST%
echo   cwd   %CD%
echo.
echo   --- 依赖 ---
call :probe ".venv\Scripts\python.exe" 虚拟环境
call :probe "web\node_modules" 前端依赖
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
".venv\Scripts\python.exe" -c "import importlib.metadata as m, amane; print('   amane ' + m.version('amane') + '  import ok')"
if errorlevel 1 goto :doctor_import_failed
goto :doctor_end

:doctor_no_venv
echo   [缺失] 虚拟环境不存在, 请运行: start.bat setup
goto :doctor_end

:doctor_import_failed
echo   [失败] amane 导入失败, 请运行: start.bat setup
goto :doctor_end

:doctor_end
echo.
echo   --- 体检结束 ---
goto :done

rem ============================================================
rem  子程序
rem ============================================================

rem 用法: call :probe "路径" "描述"
:probe
if exist "%~1" echo   [OK]   %~2
if not exist "%~1" echo   [缺失] %~2  -- 请运行 start.bat setup
exit /b 0

rem 用法: call :uvlock_state
:uvlock_state
git diff --quiet -- uv.lock
if errorlevel 1 goto :uvlock_dirty
echo   [干净] uv.lock 与 HEAD 一致
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
endlocal
exit /b 1

:bye
endlocal
exit /b 0
