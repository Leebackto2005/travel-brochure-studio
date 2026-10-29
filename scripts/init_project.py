#!/usr/bin/env python3
"""Create the standard brochure project folders without touching existing projects."""
import argparse
import csv
from pathlib import Path
import sys


FOLDERS = (
    '01_原始资料/01_行程与商务资料',
    '01_原始资料/02_参考模板与海报',
    '02_图片素材/01_景点',
    '02_图片素材/02_酒店',
    '02_图片素材/03_餐食',
    '02_图片素材/04_车辆',
    '02_图片素材/05_文化与活动',
    '02_图片素材/06_其他',
    '02_图片素材/07_处理后图片',
    '03_制作工作区/01_内容整理',
    '03_制作工作区/02_编辑源文件',
    '03_制作工作区/03_预览与审阅',
    '03_制作工作区/04_核对报告',
    '03_制作工作区/05_临时文件',
    '04_交付文件',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project_dir', type=Path, help='new project directory; its parent must already exist')
    args = parser.parse_args()
    root = args.project_dir
    try:
        # Never adopt an existing project implicitly, even if it is empty.
        root.mkdir()
    except OSError as error:
        print(f'Cannot create new project directory {root}: {error}', file=sys.stderr)
        return 2
    try:
        for folder in FOLDERS:
            (root / folder).mkdir(parents=True, exist_ok=True)
        with (root / '02_图片素材' / '素材台账.csv').open('x', encoding='utf-8-sig', newline='') as file:
            csv.writer(file).writerow(['素材ID', '类别', '主体与内容', '原始路径', '项目内路径', '来源类型', '来源网址',
                                       '作者', '许可名称', '许可链接', '使用范围', '核对日期', '使用位置', '裁切与焦点', '确认状态', '参考说明', 'SHA256'])
    except OSError as error:
        print(f'Initialization incomplete at {root}; inspect the folders already created: {error}', file=sys.stderr)
        return 2
    print(f'Created brochure project: {root.resolve()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
