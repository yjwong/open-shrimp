# vznc test/build helpers.
#
# `go test` builds an unsigned binary; the _VZVNCServer SPI works without
# entitlements for the no-VM listener path (it doesn't gate on
# com.apple.security.virtualization), but we ship the codesigned target
# anyway so callers know the production codepath.

GO ?= go

.PHONY: test test-signed build-test clean

# Plain unsigned test run. Sufficient for TestAvailable and the no-VM
# TestStartListensOnPort path.
test:
	$(GO) test -v ./...

# Build the test binary, ad-hoc codesign with the same entitlements
# Lima's limactl ships, then run it. Use this if the unsigned path ever
# starts failing on a future macOS.
build-test:
	$(GO) test -c -o vznc.test ./

test-signed: build-test
	codesign --entitlements vznc.entitlements -s - vznc.test
	./vznc.test -test.v

clean:
	rm -f vznc.test
