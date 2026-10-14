package oracle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.Test;
import reposense.model.RepoConfiguration;
import reposense.model.RepoLocation;
import reposense.parser.RepoConfigCsvParser;

class RepoConfigurationBehaviorOracleTest {
        @Test
        void csvParsingPreservesPublicConfigurationValues() throws Exception {
            Path csv = Path.of("src/test/resources/RepoConfigParserTest/"
                    + "repoconfig_noSpecialCharacter_test.csv");
            List<RepoConfiguration> configs = new RepoConfigCsvParser(csv).parse();
            assertEquals(1, configs.size());
            RepoConfiguration config = configs.get(0);
            assertEquals(new RepoLocation("https://github.com/reposense/testrepo-Beta.git"),
                    config.getLocation());
            assertEquals("master", config.getBranch());
            assertEquals(100000, config.getFileSizeLimit());
            assertTrue(config.isStandaloneConfigIgnored());
            assertTrue(config.isShallowCloningPerformed());
            assertFalse(config.isFileSizeLimitIgnored());
        }
}
