FROM ubuntu:22.04@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    git curl wget build-essential cmake automake autoconf libtool pkg-config \
    bison flex g++ gcc clang llvm \
    libgc-dev libgmp-dev \
    libboost-dev libboost-system-dev libboost-filesystem-dev libboost-thread-dev \
    libboost-program-options-dev libboost-python-dev libboost-iostreams-dev \
    libboost-graph-dev \
    libevent-dev libpcap-dev libssl-dev libffi-dev zlib1g-dev libxxhash-dev libjsoncpp-dev \
    libthrift-dev thrift-compiler libnanomsg-dev \
    python3 python3-pip python3-dev python3-setuptools python3-wheel python3-scapy \
    iproute2 iputils-ping tcpdump net-tools sudo ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

RUN git init /opt/behavioral-model && cd /opt/behavioral-model && \
    git fetch --depth 1 https://github.com/p4lang/behavioral-model.git 0bf80968bbd533e5bb58f849743001ae8221b715 && \
    git checkout --detach FETCH_HEAD && git submodule update --init --recursive --depth 1
WORKDIR /opt/behavioral-model
RUN ./autogen.sh && ./configure && make -j"$(nproc)" && make install
RUN ldconfig

RUN git init /opt/p4c && cd /opt/p4c && \
    git fetch --depth 1 https://github.com/p4lang/p4c.git 652e4056a0a4d73961f3439bdea46b2875c7886d && \
    git checkout --detach FETCH_HEAD && git submodule update --init --recursive --depth 1
WORKDIR /opt/p4c
RUN mkdir -p build
WORKDIR /opt/p4c/build
RUN cmake .. && make -j"$(nproc)" && make install
RUN ldconfig

RUN pip3 install --no-cache-dir thrift ipaddr

WORKDIR /work
CMD ["/bin/bash"]
