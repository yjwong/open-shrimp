# Builds the control client and supervisor with swiftc directly.
#
# Not SwiftPM and not xcodebuild: neither is needed to compile a set of source
# files against Foundation and Network, and requiring Xcode to exercise the
# supervisor would put its verification behind a 15 GB download.  The app
# target that wraps these sources brings its own build system.

# A toolchain installed into the user's home takes precedence over the one in
# Command Line Tools, whose compiler can be older than the SDK beside it — a
# pairing that fails while parsing the standard library's own interface, before
# it ever reaches these sources.
TOOLCHAIN ?= $(firstword $(wildcard $(HOME)/Library/Developer/Toolchains/swift-latest.xctoolchain))
SWIFTC    ?= $(if $(TOOLCHAIN),$(TOOLCHAIN)/usr/bin/swiftc,xcrun swiftc)
SDK       ?= $(shell xcrun --show-sdk-path)
DEPLOY    ?= 13.0
BUILD     ?= build

CORE_SOURCES  := $(wildcard Sources/OpenShrimpCore/*.swift)
PROBE_SOURCES := $(wildcard Sources/probe/*.swift)
SOURCES       := $(CORE_SOURCES) $(PROBE_SOURCES)

# Swift 5 language mode: the sources are written to be concurrency-safe, but
# the toolchain that has to build them is whatever the machine has.
FLAGS := -swift-version 5 -O -sdk $(SDK) -framework Foundation -framework Network

.PHONY: all probe universal clean

all: probe

probe: $(BUILD)/openshrimp-probe

$(BUILD)/openshrimp-probe: $(SOURCES)
	@mkdir -p $(BUILD)
	$(SWIFTC) $(FLAGS) -target $(shell uname -m)-apple-macos$(DEPLOY) -o $@ $(SOURCES)

# The app ships universal because the core does; building both here is what
# catches an arm64-only assumption before it reaches a release.
universal: $(SOURCES)
	@mkdir -p $(BUILD)
	$(SWIFTC) $(FLAGS) -target arm64-apple-macos$(DEPLOY)  -o $(BUILD)/probe-arm64  $(SOURCES)
	$(SWIFTC) $(FLAGS) -target x86_64-apple-macos$(DEPLOY) -o $(BUILD)/probe-x86_64 $(SOURCES)
	lipo -create -output $(BUILD)/openshrimp-probe $(BUILD)/probe-arm64 $(BUILD)/probe-x86_64
	lipo -info $(BUILD)/openshrimp-probe

clean:
	rm -rf $(BUILD)
