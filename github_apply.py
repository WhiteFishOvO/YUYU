#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 消息桥接补丁：把「她的消息 → 小鲸原生气泡」的改动应用到源码树。

用法：python github_apply.py <源码目录>
可重复执行（已打过补丁时跳过）。
"""
import os
import sys

RUST_SRC = r'''
//! QQ 消息桥接（QQ Agent → 桌宠气泡）
//!
//! 只读地轮询 QQ Agent 的私聊消息记录文件，一旦发现符合条件的新消息，
//! 就用原生台词气泡显示出来（事件推给前端，前端调用 `DSW.bubble.showDialogueLine`）。
//!
//! 显示谁的发言由常量 `SHOW_WHOSE` 决定（默认 "me"，即机器人自己说的话）。
//!
//! 设计约束：
//! - **只读**：不写、不改 QQ Agent 的任何文件，也不影响它运行；
//! - **不补历史**：启动时只记录当前最大消息 id，之后的新消息才推送；
//! - **失败静默**：文件不存在 / 解析失败时不影响桌宠其它功能。

use std::fs;
use std::path::PathBuf;
use std::thread;
use std::time::Duration;

use tauri::{AppHandle, Manager};

/// QQ Agent 私聊消息记录文件（可用环境变量 `QQ_AGENT_MSG_FILE` 覆盖）。
const DEFAULT_MESSAGE_FILE: &str =
    r"C:\Users\17703\AppData\Local\Programs\QQ Agent\data\messages\private_3299144709.json";

/// 推给前端的事件名。
const EVENT_NAME: &str = "qq-message";

/// 轮询间隔。
const POLL_INTERVAL: Duration = Duration::from_millis(1000);

/// 单条消息最多保留的字符数（防止超长文本把气泡撑爆）。
const MAX_CHARS: usize = 160;

/// 显示谁的发言：
/// - `"me"`   = 机器人自己发的（消息里 `self: true`，昵称「我」）—— **当前默认**；
/// - `"her"`  = 对方（她）发来的（`self: false`，昵称「猫」）；
/// - `"both"` = 两边都要。
const SHOW_WHOSE: &str = "me";

/// 启动桥接线程（进程内唯一，随进程退出而结束）。
pub fn spawn(handle: AppHandle) {
    thread::spawn(move || {
        let path = PathBuf::from(
            std::env::var("QQ_AGENT_MSG_FILE")
                .unwrap_or_else(|_| DEFAULT_MESSAGE_FILE.to_string()),
        );

        let mut last_seen: i64 = 0;
        let mut last_mtime: Option<u64> = None;
        let mut primed = false;

        loop {
            if let Ok(meta) = fs::metadata(&path) {
                let mtime = meta
                    .modified()
                    .ok()
                    .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                    .map(|d| d.as_secs())
                    .unwrap_or(0);

                // 只在文件被改写过时才解析，避免每秒解析 1MB+ 的 JSON。
                if last_mtime != Some(mtime) {
                    last_mtime = Some(mtime);
                    if let Ok(raw) = fs::read(&path) {
                        if let Ok(value) = serde_json::from_slice::<serde_json::Value>(&raw) {
                            for text in collect_fresh(&value, &mut last_seen, &mut primed) {
                                let _ = handle.emit(EVENT_NAME, text);
                            }
                        }
                    }
                }
            }
            thread::sleep(POLL_INTERVAL);
        }
    });
}

/// 取出「上次之后、由对方发来」的消息文本，并把游标推进到最新 id。
fn collect_fresh(
    value: &serde_json::Value,
    last_seen: &mut i64,
    primed: &mut bool,
) -> Vec<String> {
    let items = match value.get("messages").and_then(|v| v.as_array()) {
        Some(items) => items,
        None => return Vec::new(),
    };

    let mut rows: Vec<(i64, String)> = Vec::new();
    for item in items {
        // `self: true` = 机器人自己发的内容（昵称「我」）；false = 对方（她）发来的。
        let is_self = item.get("self").and_then(|v| v.as_bool()).unwrap_or(false);
        let keep = match SHOW_WHOSE {
            "her" => !is_self,
            "both" => true,
            _ => is_self, // "me"
        };
        if !keep {
            continue;
        }
        let id = item.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
        rows.push((id, render_body(item)));
    }
    rows.sort_by_key(|row| row.0);

    let max_id = rows.iter().map(|row| row.0).max().unwrap_or(0);

    // 首次只对齐游标，不补历史消息。
    if !*primed {
        *primed = true;
        *last_seen = max_id;
        return Vec::new();
    }

    let fresh: Vec<String> = rows
        .into_iter()
        .filter(|row| row.0 > *last_seen)
        .map(|row| row.1)
        .collect();
    if max_id > *last_seen {
        *last_seen = max_id;
    }
    fresh
}

/// 把一条消息渲染成气泡文本（正文 + 附件占位）。
fn render_body(item: &serde_json::Value) -> String {
    let text = item
        .get("text")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .replace('\r', " ")
        .replace('\n', " ")
        .trim()
        .to_string();

    let mut tags: Vec<&str> = Vec::new();
    if let Some(media) = item.get("media").and_then(|v| v.as_array()) {
        for entry in media {
            let kind = entry.get("kind").and_then(|v| v.as_str()).unwrap_or("");
            let tag: &str = match kind {
                "image" => "[图片]",
                "voice" | "record" | "audio" => "[语音]",
                "video" => "[视频]",
                "file" => "[文件]",
                "" => continue,
                other => other,
            };
            tags.push(tag);
        }
    }

    let mut body = if tags.is_empty() {
        text
    } else if text.is_empty() {
        tags.join("")
    } else {
        format!("{} {}", text, tags.join(""))
    };

    if body.is_empty() {
        body = "[空消息]".to_string();
    }
    if body.chars().count() > MAX_CHARS {
        body = body.chars().take(MAX_CHARS).collect::<String>() + "…";
    }
    body
}
'''


def read(p):
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write(p, s):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)


def nl_of(s):
    return "\r\n" if "\r\n" in s else "\n"


ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

# 1) 新增 shell/qq_bridge.rs
tgt = os.path.join(ROOT, "src-tauri", "src", "shell", "qq_bridge.rs")
write(tgt, RUST_SRC.lstrip("\n"))
print("[1] qq_bridge.rs ->", tgt)

# 2) 注册模块
p = os.path.join(ROOT, "src-tauri", "src", "shell", "mod.rs")
s = read(p)
if "pub mod qq_bridge;" not in s:
    s = s.replace("pub mod tray;", "pub mod qq_bridge;\npub mod tray;", 1)
    write(p, s)
    print("[2] shell/mod.rs registered")
else:
    print("[2] shell/mod.rs skip")

# 3) 启动时拉起桥接
p = os.path.join(ROOT, "src-tauri", "src", "lib.rs")
s = read(p)
if "qq_bridge::spawn" not in s:
    nl = nl_of(s)
    anchor = "            Ok(())" + nl + "        })"
    if s.count(anchor) != 1:
        sys.exit("lib.rs anchor not unique, abort")
    inject = nl.join([
        "            // QQ 消息桥接：启动后轮询 QQ Agent 的私聊消息，",
        "            // 收到新消息即用原生台词气泡显示（失败只记日志，不影响其它功能）。",
        "            shell::qq_bridge::spawn(app.handle().clone());",
        "",
        "",
    ])
    write(p, s.replace(anchor, inject + anchor, 1))
    print("[3] lib.rs injected")
else:
    print("[3] lib.rs skip")

# 4) 前端监听事件
p = os.path.join(ROOT, "frontend", "js", "main.js")
s = read(p)
if "qq-message" not in s:
    nl = nl_of(s)
    anchor = "  DSW.main = {};"
    if s.count(anchor) != 1:
        sys.exit("main.js anchor not unique, abort")
    inject = nl.join([
        "  // QQ 消息桥接：后端推来她的新消息时，立刻用原生台词气泡显示。",
        "  if (window.__TAURI__ && window.__TAURI__.event) {",
        '    window.__TAURI__.event.listen("qq-message", function (e) {',
        "      var text = e.payload;",
        "      if (text && window.DSW && window.DSW.bubble && window.DSW.bubble.showDialogueLine) {",
        "        window.DSW.bubble.showDialogueLine(text);",
        "      }",
        "    });",
        "  }",
        "",
        "",
    ])
    write(p, s.replace(anchor, inject + anchor, 1))
    print("[4] main.js injected")
else:
    print("[4] main.js skip")

print("patch done")
