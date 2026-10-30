<#
.SYNOPSIS
    在当前终端设置 LLM 接入的环境变量。

.DESCRIPTION
    凭据只在当前进程的内存里，不写任何文件。
    关掉这个终端窗口就没了，下次重新执行一次即可。

    用法（注意开头有点和空格，表示在当前会话里执行）：
        . .\set_llm.ps1 glm sk-你的key
        . .\set_llm.ps1 qwen sk-你的key -Model qwen-plus
        . .\set_llm.ps1 local

.PARAMETER Provider
    glm / qwen / deepseek / local

.PARAMETER Key
    API key。local 可省略（默认 ollama）。

.PARAMETER Model
    覆盖默认模型名。
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('glm', 'qwen', 'deepseek', 'local')]
    [string]$Provider,

    [Parameter(Position = 1)]
    [string]$Key = '',

    [string]$Model = ''
)

switch ($Provider) {
    'glm' {
        $env:LLM_BASE_URL = 'https://open.bigmodel.cn/api/paas/v4'
        if (-not $Model) { $Model = 'glm-4-flash' }
    }
    'qwen' {
        $env:LLM_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
        # 固定快照版，避免 latest 静默升级导致结果不可复现
        if (-not $Model) { $Model = 'qwen3.5-plus-2026-02-15' }
    }
    'deepseek' {
        $env:LLM_BASE_URL = 'https://api.deepseek.com/v1'
        if (-not $Model) { $Model = 'deepseek-chat' }
    }
    'local' {
        $env:LLM_BASE_URL = 'http://127.0.0.1:11434/v1'
        if (-not $Model) { $Model = 'qwen2.5:7b-instruct-q4_K_M' }
    }
}
$env:LLM_MODEL = $Model

if ($Provider -eq 'local' -and -not $Key) { $Key = 'ollama' }
if (-not $Key) {
    Write-Host '缺少 API key。用法: . .\set_llm.ps1 glm sk-你的key' -ForegroundColor Yellow
    return
}
$env:LLM_API_KEY = $Key

Write-Host '已设置（仅当前终端有效）:' -ForegroundColor Green
Write-Host "  LLM_BASE_URL = $env:LLM_BASE_URL"
Write-Host "  LLM_MODEL    = $env:LLM_MODEL"
if ($Key.Length -gt 8) {
    Write-Host ("  LLM_API_KEY  = " + $Key.Substring(0, 6) + "..." + $Key.Length + " 字符")
} else {
    Write-Host '  LLM_API_KEY  = ***'
}
