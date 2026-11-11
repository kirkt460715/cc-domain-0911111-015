# 清账 企业应收核对

维护应收、付款、核销、争议和关闭期间，并提供账龄、对账单和投递记录。不同币种分别计算；已发送成品有独立摘要，不应由当前余额替代。

## 运行环境

使用 Python 3.11，核心功能依赖标准库和 SQLite。演示和测试只使用本地合成数据，不需要账户、设备或外部运行服务。请从项目根目录执行命令。

## 检查与运行

构建检查验证语法，测试使用临时数据目录，不会连接远端服务。

```sh
python3 -m compileall -q receivables
python3 -m unittest discover -s tests -v
python3 -m receivables demo
```

演示会展示产品的实际数据处理和输出，运行结束自行退出。查看现有接口：

```sh
python3 -m receivables --help
```

## 目录与数据

receivables/ 保存领域逻辑和请求入口，tests/ 包含正常行为、边界与恢复测试。例子中的标识和记录为合成资料，不包含真实人员或生产测量。业务目录和本地数据库由调用者指定，应与源码分开保存。

## 干净环境复现

容器定义固定运行时版本。构建时准备环境，运行时无需网络；每次启动会复制只读源码到临时工作目录，测试产生的文件不会写回仓库。

```sh
docker build -t local-product-check .
docker run --rm --network none local-product-check
```
