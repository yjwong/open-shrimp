# Builds the control client, the supervisor and the menu bar app with swiftc
# directly.
#
# Not SwiftPM and not xcodebuild: neither is needed to compile a set of source
# files against Foundation, Network and AppKit, and requiring Xcode to exercise
# the supervisor would put its verification behind a 15 GB download.  An .app is
# a directory with an Info.plist in it, so the same holds for the bundle — the
# one Xcode-only tool a menu bar app might have needed is `actool`, and loading
# the icon from loose PNGs avoids it.

# A toolchain installed into the user's home takes precedence over the one in
# Command Line Tools, whose compiler can be older than the SDK beside it — a
# pairing that fails while parsing the standard library's own interface, before
# it ever reaches these sources.
TOOLCHAIN ?= $(firstword $(wildcard $(HOME)/Library/Developer/Toolchains/swift-latest.xctoolchain))
SWIFTC    ?= $(if $(TOOLCHAIN),$(TOOLCHAIN)/usr/bin/swiftc,xcrun swiftc)
SDK       ?= $(shell xcrun --show-sdk-path)
DEPLOY    ?= 13.0
BUILD     ?= build

ICONS   ?= App/Resources
VERSION ?= $(shell cat ../VERSION)

CORE_SOURCES  := $(wildcard Sources/OpenShrimpCore/*.swift)
PROBE_SOURCES := $(wildcard Sources/probe/*.swift)
APP_SOURCES   := $(wildcard Sources/app/*.swift)
SOURCES       := $(CORE_SOURCES) $(PROBE_SOURCES)

# Swift 5 language mode: the sources are written to be concurrency-safe, but
# the toolchain that has to build them is whatever the machine has.
FLAGS := -swift-version 5 -O -sdk $(SDK) -framework Foundation -framework Network
APP_FLAGS := $(FLAGS) -framework AppKit -framework SwiftUI -framework UserNotifications \
	-framework ServiceManagement

APP     := $(BUILD)/OpenShrimp.app
APP_BIN := $(APP)/Contents/MacOS/OpenShrimp

.PHONY: all probe app universal app-universal clean

all: probe app

probe: $(BUILD)/openshrimp-probe

app: $(APP)

$(BUILD)/openshrimp-probe: $(SOURCES)
	@mkdir -p $(BUILD)
	$(SWIFTC) $(FLAGS) -target $(shell uname -m)-apple-macos$(DEPLOY) -o $@ $(SOURCES)

$(APP): $(CORE_SOURCES) $(APP_SOURCES) App/Info.plist ../VERSION
	@rm -rf $(APP)
	@mkdir -p $(APP)/Contents/MacOS $(APP)/Contents/Resources
	$(SWIFTC) $(APP_FLAGS) -target $(shell uname -m)-apple-macos$(DEPLOY) \
		-o $(APP_BIN) $(CORE_SOURCES) $(APP_SOURCES)
	$(MAKE) bundle

# Everything about the bundle except the executable, so that `app` and
# `app-universal` differ only in how the executable was produced.
.PHONY: bundle
bundle:
	sed 's/@VERSION@/$(VERSION)/g' App/Info.plist > $(APP)/Contents/Info.plist
	cp $(ICONS)/menubar-icon.png $(ICONS)/menubar-icon@2x.png $(ICONS)/icon.icns \
		$(APP)/Contents/Resources/
	# Ad-hoc signed even for a local build: both the login item and the
	# notification centre attribute their state to a code signature, and an
	# unsigned bundle gets a new identity on every rebuild — so Start at Login
	# silently stops applying to the app that registered it.
	codesign --force --sign - --identifier com.openshrimp.app $(APP)

# The app ships universal because the core does; building both here is what
# catches an arm64-only assumption before it reaches a release.
universal: $(SOURCES)
	@mkdir -p $(BUILD)
	$(SWIFTC) $(FLAGS) -target arm64-apple-macos$(DEPLOY)  -o $(BUILD)/probe-arm64  $(SOURCES)
	$(SWIFTC) $(FLAGS) -target x86_64-apple-macos$(DEPLOY) -o $(BUILD)/probe-x86_64 $(SOURCES)
	lipo -create -output $(BUILD)/openshrimp-probe $(BUILD)/probe-arm64 $(BUILD)/probe-x86_64
	lipo -info $(BUILD)/openshrimp-probe

app-universal: $(CORE_SOURCES) $(APP_SOURCES) App/Info.plist ../VERSION
	@rm -rf $(APP)
	@mkdir -p $(APP)/Contents/MacOS $(APP)/Contents/Resources
	$(SWIFTC) $(APP_FLAGS) -target arm64-apple-macos$(DEPLOY) \
		-o $(BUILD)/OpenShrimp-arm64 $(CORE_SOURCES) $(APP_SOURCES)
	$(SWIFTC) $(APP_FLAGS) -target x86_64-apple-macos$(DEPLOY) \
		-o $(BUILD)/OpenShrimp-x86_64 $(CORE_SOURCES) $(APP_SOURCES)
	lipo -create -output $(APP_BIN) $(BUILD)/OpenShrimp-arm64 $(BUILD)/OpenShrimp-x86_64
	lipo -info $(APP_BIN)
	$(MAKE) bundle

clean:
	rm -rf $(BUILD)
