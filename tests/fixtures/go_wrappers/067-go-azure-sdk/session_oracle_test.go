package sessionoracle

import (
	"context"
	"fmt"
	"net/http"
	"testing"

	"github.com/hashicorp/go-azure-sdk/sdk/auth"
	"github.com/hashicorp/go-azure-sdk/sdk/environments"
)

func TestSessionAzureCLIToken(t *testing.T) {
	authorizer, err := auth.NewAzureCliAuthorizer(
		context.Background(),
		auth.AzureCliAuthorizerOptions{
			Api:      environments.AzurePublic().MicrosoftGraph,
			TenantId: "session-tenant",
		},
	)
	if err != nil {
		t.Fatalf("create authorizer: %v", err)
	}

	token, err := authorizer.Token(context.Background(), &http.Request{})
	if err != nil {
		t.Fatalf("obtain token: %v", err)
	}
	if token.AccessToken != "session-access-token" {
		t.Fatalf("access token = %q", token.AccessToken)
	}
	if token.TokenType != "Bearer" {
		t.Fatalf("token type = %q", token.TokenType)
	}
	if token.Expiry.Year() != 2030 {
		t.Fatalf("expiry = %s", token.Expiry)
	}

	fmt.Println(`P2P_SIGNATURE={"access_token":"session-access-token","expiry_year":2030,"tenant":"session-tenant","token_type":"Bearer"}`)
}
