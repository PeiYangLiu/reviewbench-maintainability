package sessionoracle

import (
	"fmt"
	"testing"
	"time"

	"cloud.google.com/go/bigtable"
)

func TestSessionGarbageCollectionPolicyBehavior(t *testing.T) {
	maxAge := bigtable.MaxAgePolicy(48 * time.Hour)
	maxVersions := bigtable.MaxVersionsPolicy(3)
	union := bigtable.UnionPolicy(maxAge, maxVersions)
	intersection := bigtable.IntersectionPolicy(maxAge, maxVersions)

	if got, want := maxAge.String(), "age() > 2d"; got != want {
		t.Fatalf("max-age string = %q, want %q", got, want)
	}
	if got, want := maxVersions.String(), "versions() > 3"; got != want {
		t.Fatalf("max-versions string = %q, want %q", got, want)
	}
	if got, want := union.String(), "(age() > 2d || versions() > 3)"; got != want {
		t.Fatalf("union string = %q, want %q", got, want)
	}
	if got, want := intersection.String(), "(age() > 2d && versions() > 3)"; got != want {
		t.Fatalf("intersection string = %q, want %q", got, want)
	}
	fmt.Println(`P2P_SIGNATURE={"intersection":"(age() > 2d && versions() > 3)","max_age":"age() > 2d","max_versions":"versions() > 3","union":"(age() > 2d || versions() > 3)"}`)
}
