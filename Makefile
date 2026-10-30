CC      ?= clang
CFLAGS  ?= -O2
PREFIX  ?= /usr/local
MCACHE  := build/mc
TMPDIR  := $(CURDIR)/build/tmp

all: cu cu-ax

build:
	@mkdir -p $(MCACHE) $(TMPDIR)

cu: cu.c | build
	TMPDIR=$(TMPDIR) $(CC) $(CFLAGS) -fmodules-cache-path=$(MCACHE) -o $@ $< \
		-framework ApplicationServices -framework CoreFoundation

cu-ax: cu_ax.m | build
	TMPDIR=$(TMPDIR) $(CC) $(CFLAGS) -fobjc-arc -fmodules-cache-path=$(MCACHE) -o $@ $< \
		-framework ApplicationServices -framework AppKit -framework CoreFoundation

install: all
	@mkdir -p $(PREFIX)/bin
	install -m 755 cu cu-ax $(PREFIX)/bin/
	install -m 755 cu_act.py $(PREFIX)/bin/cu-act

clean:
	rm -rf build cu cu-ax

.PHONY: all build install clean
