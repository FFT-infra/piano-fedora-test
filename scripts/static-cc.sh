#!/bin/sh
# 给上游 host builder 提供静态链接编译器；实际编译仍在 Actions。
exec cc -static "$@"
