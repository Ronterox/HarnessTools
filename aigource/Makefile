CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -std=c11
RAYLIB  := vendor/raylib-5.5_linux_amd64
YYJSON  := vendor/yyjson-0.10.0/src
CPPFLAGS += -D_GNU_SOURCE -I$(RAYLIB)/include -I$(YYJSON)
LDLIBS  := $(RAYLIB)/lib/libraylib.a -lGL -lX11 -lm -lpthread -ldl -lrt

STAMP   := $(RAYLIB)/lib/libraylib.a $(YYJSON)/yyjson.c
SRC := src/main.c src/util.c src/event.c src/transcript.c src/ingest.c src/model.c src/render.c src/timeline.c src/watch.c
OBJ := $(SRC:src/%.c=build/%.o) build/yyjson.o

build/aigource: $(OBJ)
	$(CC) $(CFLAGS) -o $@ $(OBJ) $(LDLIBS)

release: build/aigource
	strip build/aigource

build/%.o: src/%.c src/*.h $(STAMP) | build
	$(CC) $(CFLAGS) $(CPPFLAGS) -c -o $@ $<

build/yyjson.o: $(STAMP) | build
	$(CC) $(CFLAGS) -w -c -o $@ $(YYJSON)/yyjson.c

$(STAMP):
	./scripts/fetch-deps.sh

build:
	mkdir -p build

test: build/aigource
	./tests/run.sh

test-watch: build/aigource
	./tests/watch_test.sh

clean:
	rm -rf build

.PHONY: test test-watch clean release
