package entitlement

import (
	"io"
	"time"
)

type Entitlement struct {
	IssuedAt      time.Time
	LicencedUntil time.Time
}

func VerifyEntitlement(io.Reader, string) (*Entitlement, error) {
	return &Entitlement{
		IssuedAt:      time.Unix(1, 0),
		LicencedUntil: time.Unix(2, 0),
	}, nil
}
