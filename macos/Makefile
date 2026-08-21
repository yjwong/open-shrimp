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

# Sparkle, fetched and pinned rather than resolved by a package manager.  Its
# SwiftPM package is a binaryTarget, and SwiftPM neither embeds nor code-signs
# a framework into an app bundle, so the copy and the signature below would
# survive adopting it — see the header for what that costs.
#
# The release tarball ships an already-universal Sparkle.framework; the
# .xcframework only exists in the SwiftPM zip.  So one copy serves both arch
# passes and there is no slice to select.
#
# The checksum is the pin.  Bumping the version without bumping it stops the
# build rather than silently building against whatever the URL now serves.
SPARKLE_VERSION := 2.9.6
SPARKLE_SHA256  := 52bf9e88cdd972fc0c81501377a880e90d47031bd8ca5462488f843e2609e192
SPARKLE_URL     := https://github.com/sparkle-project/Sparkle/releases/download/$(SPARKLE_VERSION)/Sparkle-$(SPARKLE_VERSION).tar.xz
VENDOR          := vendor
SPARKLE         := $(VENDOR)/Sparkle.framework
# Named for the version, so a bump is a different file and refetches.
SPARKLE_STAMP   := $(VENDOR)/.sparkle-$(SPARKLE_VERSION)

CORE_SOURCES  := $(wildcard Sources/OpenShrimpCore/*.swift)
PROBE_SOURCES := $(wildcard Sources/probe/*.swift)
APP_SOURCES   := $(wildcard Sources/app/*.swift)
SOURCES       := $(CORE_SOURCES) $(PROBE_SOURCES)

# Swift 5 language mode: the sources are written to be concurrency-safe, but
# the toolchain that has to build them is whatever the machine has.
FLAGS := -swift-version 5 -O -sdk $(SDK) -framework Foundation -framework Network
# The rpath is not optional: the executable is copied out of the build
# directory into the bundle, and without it dyld has nowhere to find the
# framework beside it — which shows up as an app that will not launch at all.
APP_FLAGS := $(FLAGS) -framework AppKit -framework SwiftUI -framework UserNotifications \
	-framework ServiceManagement \
	-F $(VENDOR) -framework Sparkle \
	-Xlinker -rpath -Xlinker @executable_path/../Frameworks

APP     := $(BUILD)/OpenShrimp.app
APP_BIN := $(APP)/Contents/MacOS/OpenShrimp

.PHONY: all probe app universal app-universal sparkle clean

all: probe app

probe: $(BUILD)/openshrimp-probe

app: $(APP)

sparkle: $(SPARKLE_STAMP)

# Fetched by the app targets rather than by hand, so a fresh clone builds with
# `make app` and a developer and CI fetch the same bytes the same way.  The
# probe links none of this and stays as it was.
$(SPARKLE_STAMP):
	@mkdir -p $(VENDOR)
	@rm -rf $(SPARKLE) $(VENDOR)/bin
	curl -fsSL -o $(VENDOR)/Sparkle.tar.xz $(SPARKLE_URL)
	echo "$(SPARKLE_SHA256)  $(VENDOR)/Sparkle.tar.xz" | shasum -a 256 -c -
	# The tools as well as the framework: `generate_keys`, `sign_update` and
	# `generate_appcast` have to be the same Sparkle version as the framework
	# that verifies what they sign, and one checksum over one tarball is what
	# makes that true without a second thing to keep in step.
	tar -xJf $(VENDOR)/Sparkle.tar.xz -C $(VENDOR) ./Sparkle.framework ./bin
	@rm $(VENDOR)/Sparkle.tar.xz
	@touch $@

$(BUILD)/openshrimp-probe: $(SOURCES)
	@mkdir -p $(BUILD)
	$(SWIFTC) $(FLAGS) -target $(shell uname -m)-apple-macos$(DEPLOY) -o $@ $(SOURCES)

$(APP): $(CORE_SOURCES) $(APP_SOURCES) App/Info.plist ../VERSION $(SPARKLE_STAMP)
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
	# `ditto` rather than `cp -R`: a framework is a tree of symlinks into
	# Versions/, and the copy has to still be one.
	@mkdir -p $(APP)/Contents/Frameworks
	@rm -rf $(APP)/Contents/Frameworks/Sparkle.framework
	ditto $(SPARKLE) $(APP)/Contents/Frameworks/Sparkle.framework
	# Ad-hoc signed even for a local build: both the login item and the
	# notification centre attribute their state to a code signature, and an
	# unsigned bundle gets a new identity on every rebuild — so Start at Login
	# silently stops applying to the app that registered it.  The same script
	# CI runs, with the same walk over the nested code, so a signing mistake
	# is found here rather than at notarization.
	Tools/sign-bundle.sh - $(APP)

# The app ships universal because the core does; building both here is what
# catches an arm64-only assumption before it reaches a release.
universal: $(SOURCES)
	@mkdir -p $(BUILD)
	$(SWIFTC) $(FLAGS) -target arm64-apple-macos$(DEPLOY)  -o $(BUILD)/probe-arm64  $(SOURCES)
	$(SWIFTC) $(FLAGS) -target x86_64-apple-macos$(DEPLOY) -o $(BUILD)/probe-x86_64 $(SOURCES)
	lipo -create -output $(BUILD)/openshrimp-probe $(BUILD)/probe-arm64 $(BUILD)/probe-x86_64
	lipo -info $(BUILD)/openshrimp-probe

app-universal: $(CORE_SOURCES) $(APP_SOURCES) App/Info.plist ../VERSION $(SPARKLE_STAMP)
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
