# 兼容窗口内的灰度发布与回撤

解析服务发布演练，流量范围由 region 和 client_cap 共同定义。观察是来源系统已聚合的计数，不是发布结论。构建编号、队列格式编号和路由代次是不同的标识。

## 在本地查看资料

运行环境使用 Python 3.11.2 和标准库。启动资料浏览器无需安装额外包。

```sh
python materials.py
python materials.py builds
python -m unittest discover -s tests -v
```

资料浏览器也接受下方的其他资料名。JSONL 使用 UTF-8 编码，每行是一个对象。表内的顺序对应来源记录，事件时间与接收时间的区别以资料说明为准。各入口可以在无外网的环境中运行。

资料字段和业务口径见[演练资料格式](materials/README.md)。
