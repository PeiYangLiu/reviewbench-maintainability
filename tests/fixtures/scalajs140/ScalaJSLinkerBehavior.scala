package reviewbench.oracle

import scala.concurrent.Await
import scala.concurrent.ExecutionContext.Implicits.global
import scala.concurrent.duration.Duration

import org.scalajs.linker.MemOutputDirectory
import org.scalajs.linker.StandardImpl
import org.scalajs.linker.interface.LinkingException
import org.scalajs.linker.interface.StandardConfig
import org.scalajs.logging.NullLogger

object ScalaJSLinkerBehavior {
  def main(args: Array[String]): Unit = {
    val output = MemOutputDirectory()
    val failure =
      try {
        Await.result(
          StandardImpl
            .linker(StandardConfig())
            .link(Nil, Nil, output, NullLogger),
          Duration.Inf,
        )
        throw new AssertionError("empty input unexpectedly linked")
      } catch {
        case error: LinkingException => error
      }

    require(failure.getMessage.nonEmpty)
    require(output.fileNames().isEmpty)
    println("empty public linker input is rejected without output")
  }
}
